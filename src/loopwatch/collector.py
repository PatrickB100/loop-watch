import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loopwatch.detectors.context import DetectorContext, new_log_lines
from loopwatch.detectors.runner import run_detectors
from loopwatch.detectors.storm import baseline_from_history
from loopwatch.inventory import Inventory, Switch
from loopwatch.model import Event, Snapshot
from loopwatch.profiles import profile_for
from loopwatch.report.ranking import uplinks_from_neighbors
from loopwatch.store import Store
from loopwatch.transport import Transport, TransportError

log = logging.getLogger("loopwatch")

ESCALATED_INTERVAL = 10  # seconds between polls while something is going on
ESCALATION_DURATION = timedelta(seconds=600)
BASELINE_WINDOW = timedelta(hours=1)
RECENT_WINDOW = timedelta(minutes=5)
UNREACHABLE_WINDOW = timedelta(minutes=2)  # two switches down within this window is more than a single hiccup
PRUNE_INTERVAL = timedelta(hours=24)
MAX_SESSIONS = 8
CHECK_INTERVAL = 30.0  # seconds between looks at the inventory file's mtime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _managed_neighbors(snapshot: Snapshot, inv: Inventory) -> set[str]:
    names: set[str] = set()
    for n in snapshot.neighbors:
        short = n.name.split(".")[0]
        if n.name in inv.names:
            names.add(n.name)
        elif short in inv.names:
            names.add(short)
    return names


def _uplink_ports(prev: Snapshot | None, inv: Inventory) -> frozenset[str]:
    if prev is None:
        return frozenset()
    return frozenset(port for _, port in uplinks_from_neighbors({prev.switch: prev.neighbors}, inv.names))


def poll_once(
    switch: Switch,
    inv: Inventory,
    transport: Transport,
    store: Store,
    prev: Snapshot | None,
    now: datetime,
) -> tuple[Snapshot | None, list[Event]]:
    """One poll of one switch: fetch, parse, detect, store. Only TransportError is handled here.

    Without an in-memory `prev` (service start, `snapshot` command) the newest stored snapshot is
    the baseline, so a restart does not re-report the whole log buffer and every known neighbour."""
    if prev is None:
        prev = store.latest_snapshot(switch.name)
    try:
        raw = transport.run(switch, inv.credentials_for(switch), switch.commands)
    except TransportError as exc:
        store.record_poll(switch.name, switch.host, now, now, None, exc.message)
        others_down = any(
            e.kind == "unreachable" and e.switch != switch.name
            for e in store.events(now - UNREACHABLE_WINDOW, now)
        )
        event = Event(now, switch.name, "unreachable", "medium" if others_down else "low", exc.message)
        store.write_events([event])
        return None, [event]

    snapshot = profile_for(switch.device_type).parse(switch.name, raw, now)
    if snapshot.hosts:
        store.upsert_hosts(snapshot.hosts, f"dhcp:{switch.name}", now)
    ctx = DetectorContext(
        prev=prev,
        cur=snapshot,
        baseline_pps=baseline_from_history(store.counter_history(switch.name, now - BASELINE_WINDOW)),
        recent_events=store.events(now - RECENT_WINDOW, now, switch.name),
        uplink_ports=_uplink_ports(prev, inv),
    )
    events = run_detectors(ctx)
    store.record_poll(switch.name, switch.host, now, now, snapshot, None, new_log_lines=new_log_lines(ctx))
    store.write_events(events)
    return snapshot, events


class Escalation:
    """Which switches are polled fast, and until when."""

    def __init__(self) -> None:
        self._until: dict[str, datetime] = {}

    def trigger(self, switch: str, neighbors: Iterable[str], now: datetime) -> None:
        until = now + ESCALATION_DURATION
        for name in (switch, *neighbors):
            self._until[name] = max(self._until.get(name, until), until)

    def interval(self, switch: str, default: int, now: datetime) -> int:
        until = self._until.get(switch)
        return ESCALATED_INTERVAL if until is not None and now <= until else default


def _escalates(events: list[Event]) -> bool:
    return any(e.severity == "high" or e.kind == "stp_topology_change" for e in events)


async def run_collector(
    load: Callable[[], Inventory],
    transport: Transport,
    store: Store,
    *,
    stop: asyncio.Event,
    reload: asyncio.Event | None = None,
    inventory_path: Path | None = None,
    check_interval: float = CHECK_INTERVAL,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_sessions: int = MAX_SESSIONS,
    escalation: Escalation | None = None,
    on_inventory_error: Callable[[str | None], None] | None = None,
) -> None:
    """Poll every device until `stop` is set, reloading the inventory when `reload` is set or the
    file changes. A device whose settings changed gets a fresh worker; unchanged ones keep running.
    A failing load keeps the running set. Any exception other than TransportError ends the run."""
    esc = escalation if escalation is not None else Escalation()
    sessions = asyncio.Semaphore(max_sessions)
    reload_event = reload if reload is not None else asyncio.Event()
    neighbors_of: dict[str, set[str]] = {}
    state: dict[str, Inventory] = {"inv": Inventory((), {}, {})}
    workers: dict[str, tuple[Switch, asyncio.Task[None]]] = {}
    retired: list[asyncio.Task[None]] = []

    async def watch(switch: Switch) -> None:
        prev: Snapshot | None = None
        while not stop.is_set():
            started = clock()
            inv = state["inv"]
            async with sessions:
                snapshot, events = await asyncio.to_thread(poll_once, switch, inv, transport, store, prev, started)
            now = clock()
            log.info(
                "poll switch=%s ok=%s events=%d duration=%.1fs",
                switch.name, snapshot is not None, len(events), (now - started).total_seconds(),
            )
            if snapshot is not None:
                prev = snapshot
                neighbors_of[switch.name] = _managed_neighbors(snapshot, inv)
            if _escalates(events):
                esc.trigger(switch.name, neighbors_of.get(switch.name, ()), now)
            await sleep(esc.interval(switch.name, switch.poll_interval, now))

    async def pruner() -> None:
        while not stop.is_set():
            await asyncio.to_thread(store.prune, clock())
            try:
                await asyncio.wait_for(stop.wait(), timeout=PRUNE_INTERVAL.total_seconds())
            except TimeoutError:
                pass

    def apply(inv: Inventory) -> None:
        wanted = {s.name: s for s in inv.switches}
        for name, (switch, task) in list(workers.items()):
            if wanted.get(name) != switch:
                task.cancel()
                retired.append(task)
                del workers[name]
                log.info("stopped watching %s", name)
        for name, switch in wanted.items():
            if name not in workers:
                workers[name] = (switch, asyncio.create_task(watch(switch), name=f"watch:{name}"))
                log.info("watching %s (%s)", name, switch.device_type)

    def try_load() -> None:
        try:
            inv = load()
        except Exception as exc:  # a broken file must not take the running collector down
            log.error("inventory not reloaded: %s", exc)
            if on_inventory_error:
                on_inventory_error(str(exc))
            return
        state["inv"] = inv
        if on_inventory_error:
            on_inventory_error(None)
        apply(inv)

    def mtime() -> int | None:
        if inventory_path is None:
            return None
        try:
            return Path(inventory_path).stat().st_mtime_ns
        except OSError:
            return None

    try_load()
    seen_mtime = mtime()
    prune_task = asyncio.create_task(pruner(), name="pruner")
    stopper = asyncio.create_task(stop.wait(), name="stop")
    try:
        while True:
            reload_wait = asyncio.create_task(reload_event.wait(), name="reload")
            tasks = [t for _, t in workers.values()] + [prune_task, stopper, reload_wait]
            done, _ = await asyncio.wait(tasks, timeout=check_interval, return_when=asyncio.FIRST_COMPLETED)
            if reload_wait not in done:
                reload_wait.cancel()
            if stopper in done:
                break
            for task in done:
                if task in (stopper, reload_wait) or task.cancelled():
                    continue
                if task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
            current_mtime = mtime()
            if reload_wait in done or current_mtime != seen_mtime:
                reload_event.clear()
                seen_mtime = current_mtime
                try_load()
    finally:
        all_tasks = [t for _, t in workers.values()] + retired + [prune_task, stopper]
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
    for _, task in workers.values():
        if task.done() and not task.cancelled() and task.exception() is not None:
            raise task.exception()  # type: ignore[misc]
