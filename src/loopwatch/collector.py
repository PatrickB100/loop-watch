import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone

from loopwatch.detectors.context import DetectorContext, new_log_lines
from loopwatch.detectors.runner import run_detectors
from loopwatch.detectors.storm import baseline_from_history
from loopwatch.inventory import Inventory, Switch
from loopwatch.model import Event, Snapshot
from loopwatch.parsers.snapshot import parse_snapshot
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
        raw = transport.run(switch, inv.credentials, switch.commands)
    except TransportError as exc:
        store.record_poll(switch.name, switch.host, now, now, None, exc.message)
        others_down = any(
            e.kind == "unreachable" and e.switch != switch.name
            for e in store.events(now - UNREACHABLE_WINDOW, now)
        )
        event = Event(now, switch.name, "unreachable", "medium" if others_down else "low", exc.message)
        store.write_events([event])
        return None, [event]

    snapshot = parse_snapshot(switch.name, raw, now)
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
    inv: Inventory,
    transport: Transport,
    store: Store,
    *,
    stop: asyncio.Event,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_sessions: int = MAX_SESSIONS,
    escalation: Escalation | None = None,
) -> None:
    """Poll every switch until `stop` is set. Any exception other than TransportError ends the run."""
    esc = escalation if escalation is not None else Escalation()
    sessions = asyncio.Semaphore(max_sessions)
    neighbors_of: dict[str, set[str]] = {}

    async def watch(switch: Switch) -> None:
        prev: Snapshot | None = None
        while not stop.is_set():
            started = clock()
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

    workers = [asyncio.create_task(watch(s), name=f"watch:{s.name}") for s in inv.switches]
    workers.append(asyncio.create_task(pruner(), name="pruner"))
    stopper = asyncio.create_task(stop.wait(), name="stop")
    try:
        while True:
            done, _ = await asyncio.wait([*workers, stopper], return_when=asyncio.FIRST_COMPLETED)
            if stopper in done or all(w.done() for w in workers):
                break
            for task in done:
                if task is not stopper and task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
            # a worker ended cleanly (stop set between checks): keep waiting for the rest
    finally:
        for task in [*workers, stopper]:
            task.cancel()
        await asyncio.gather(*workers, stopper, return_exceptions=True)
    for task in workers:
        if task.done() and not task.cancelled() and task.exception() is not None:
            raise task.exception()  # type: ignore[misc]
