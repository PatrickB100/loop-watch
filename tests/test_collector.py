import asyncio
import sqlite3
from datetime import timedelta

import pytest

from loopwatch.collector import Escalation, poll_once, run_collector
from loopwatch.inventory import Inventory, InventoryError, Switch
from loopwatch.store import Store
from loopwatch.transport import FakeTransport
from tests.conftest import FIXTURES, NOW


@pytest.fixture
def store():
    with Store(":memory:") as s:
        s.init_schema()
        yield s


@pytest.fixture
def fake(ios15_root):
    return FakeTransport(ios15_root)


@pytest.fixture
def unreachable_fake(tmp_path):
    (tmp_path / "sw-a").mkdir()
    (tmp_path / "sw-a" / "UNREACHABLE").touch()
    return FakeTransport(tmp_path)


def test_poll_once_first_poll_stores_snapshot_and_events(store, inv, fake):
    snap, events = poll_once(inv.by_name("sw-a"), inv, fake, store, None, NOW)
    # no prev: the bridge on edge port Gi1/0/14 fires, and the MACFLAP line in the log buffer counts as new
    assert snap is not None and {e.kind for e in events} == {"unexpected_neighbor", "mac_flap"}
    assert len(store.events(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))) == 2
    assert store.switches() == ["sw-a"]
    assert fake.calls == [("sw-a", inv.by_name("sw-a").commands)]


def test_poll_once_without_prev_uses_the_stored_snapshot(store, inv, fake):
    # A restart, or `snapshot` while `watch` runs, must not re-report the whole log buffer and every known bridge.
    poll_once(inv.by_name("sw-a"), inv, fake, store, None, NOW)
    snap, events = poll_once(inv.by_name("sw-a"), inv, fake, store, None, NOW + timedelta(seconds=60))
    assert snap is not None and events == []


def test_poll_once_second_poll_is_quiet(store, inv, fake):
    snap, _ = poll_once(inv.by_name("sw-a"), inv, fake, store, None, NOW)
    snap2, events = poll_once(inv.by_name("sw-a"), inv, fake, store, snap, NOW + timedelta(seconds=60))
    assert snap2 is not None and events == []


def test_poll_once_uplink_ports_come_from_prev_neighbors(store, inv, fake, monkeypatch):
    seen = {}
    import loopwatch.collector as collector

    real = collector.run_detectors

    def spy(ctx):
        seen["uplinks"] = ctx.uplink_ports
        return real(ctx)

    monkeypatch.setattr(collector, "run_detectors", spy)
    snap, _ = poll_once(inv.by_name("sw-a"), inv, fake, store, None, NOW)
    poll_once(inv.by_name("sw-a"), inv, fake, store, snap, NOW + timedelta(seconds=60))
    assert seen["uplinks"] == frozenset({"Gi1/0/48"})  # faces sw-core-1, which is in the inventory


def test_poll_once_unreachable(store, inv, unreachable_fake):
    snap, events = poll_once(inv.by_name("sw-a"), inv, unreachable_fake, store, None, NOW)
    assert snap is None and [e.kind for e in events] == ["unreachable"] and events[0].severity == "low"
    assert "hunter2" not in events[0].reason
    assert store.events(NOW - timedelta(minutes=1), NOW)[0].kind == "unreachable"


def test_poll_once_unreachable_is_medium_when_another_switch_is_down_too(store, inv, unreachable_fake):
    from loopwatch.model import Event

    store.write_events([Event(NOW - timedelta(minutes=1), "sw-core-1", "unreachable", "low", "x")])
    _, events = poll_once(inv.by_name("sw-a"), inv, unreachable_fake, store, None, NOW)
    assert events[0].severity == "medium"


def test_escalation_interval():
    e = Escalation()
    e.trigger("sw-a", ["sw-core-1"], NOW)
    assert e.interval("sw-a", 60, NOW + timedelta(seconds=599)) == 10
    assert e.interval("sw-core-1", 60, NOW) == 10
    assert e.interval("sw-a", 60, NOW + timedelta(seconds=601)) == 60
    assert e.interval("sw-b", 60, NOW) == 60


async def test_run_collector_polls_each_switch_and_stops(store, inv, fake):
    stop = asyncio.Event()
    sleeps: list[float] = []
    esc = Escalation()

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            stop.set()
        await asyncio.sleep(0)

    await run_collector(lambda: inv, fake, store, clock=lambda: NOW, sleep=fake_sleep, stop=stop, escalation=esc)
    assert {c[0] for c in fake.calls} == {"sw-a", "sw-core-1"}
    assert 10 in sleeps
    assert esc.interval("sw-a", 60, NOW) == 10  # sw-a's unexpected_neighbor escalated it
    assert esc.interval("sw-core-1", 60, NOW) == 10  # and its CDP neighbour sw-core-1 with it
    assert len(store.switches()) == 2


async def test_store_error_stops_collector(store, inv, fake, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_poll", boom)
    stop = asyncio.Event()

    async def fake_sleep(seconds):
        await asyncio.sleep(0)

    with pytest.raises(sqlite3.OperationalError):
        await run_collector(lambda: inv, fake, store, clock=lambda: NOW, sleep=fake_sleep, stop=stop)


async def test_reload_adds_removes_and_restarts_devices(store, fake, creds):
    inv = {"v": Inventory((Switch(name="sw-a", host="10.0.0.5"),), {"ssh": creds})}
    stop, reload = asyncio.Event(), asyncio.Event()

    async def fake_sleep(seconds):
        await asyncio.sleep(0)
        if len(fake.calls) == 2:  # sw-a polled twice: add sw-core-1, change sw-a's interval
            inv["v"] = Inventory((Switch(name="sw-a", host="10.0.0.5", poll_interval=30),
                                  Switch(name="sw-core-1", host="10.0.0.1")), {"ssh": creds})
            reload.set()
        if len(fake.calls) >= 6:
            stop.set()

    await run_collector(lambda: inv["v"], fake, store, stop=stop, reload=reload, clock=lambda: NOW,
                        sleep=fake_sleep, check_interval=1000)
    names = [c[0] for c in fake.calls]
    assert names[:2] == ["sw-a", "sw-a"] and "sw-core-1" in names[2:]


async def test_reload_failure_keeps_running_set(store, fake, inv):
    stop, reload = asyncio.Event(), asyncio.Event()
    errors: list[str | None] = []
    state = {"fail": False}

    def load():
        if state["fail"]:
            raise InventoryError("bad yaml")
        return inv

    async def fake_sleep(seconds):
        await asyncio.sleep(0)
        if len(fake.calls) == 2 and not state["fail"]:
            state["fail"] = True
            reload.set()
        if len(fake.calls) >= 4:
            stop.set()

    await run_collector(load, fake, store, stop=stop, reload=reload, clock=lambda: NOW, sleep=fake_sleep,
                        check_interval=1000, on_inventory_error=errors.append)
    assert errors[0] is None and "bad yaml" in (errors[-1] or "")
    assert {c[0] for c in fake.calls} == {"sw-a", "sw-core-1"}


def test_poll_once_upserts_dhcp_hosts(store, creds):
    fake = FakeTransport(FIXTURES / "routeros7")
    ccr = Switch(name="ccr", host="10.0.0.254", device_type="mikrotik_routeros", credentials="mikrotik")
    inv = Inventory((ccr,), {"mikrotik": creds})
    poll_once(ccr, inv, fake, store, None, NOW)
    assert store.hosts_for({"a4b1.c2d3.e4f5"})["a4b1.c2d3.e4f5"].hostname == "tp-link-switch"


def test_poll_once_passes_a_total_packet_baseline(store, inv, fake, monkeypatch):
    import loopwatch.collector as collector

    seen = {}
    real = collector.run_detectors

    def spy(ctx):
        seen["total"] = ctx.baseline_total_pps
        return real(ctx)

    monkeypatch.setattr(collector, "run_detectors", spy)
    for i in range(4):  # the baseline is a median of rates and needs two rates, so three earlier polls
        poll_once(inv.by_name("sw-a"), inv, fake, store, None, NOW + timedelta(seconds=60 * i))
    assert seen["total"] and all(v >= 0 for v in seen["total"].values())
