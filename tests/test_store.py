from dataclasses import replace
from datetime import timedelta

import pytest

from loopwatch.model import Event
from loopwatch.store import Store
from tests.conftest import NOW


@pytest.fixture
def store():
    with Store(":memory:") as s:
        s.init_schema()
        yield s


def replace_bcast(snap, port, value):
    ifs = tuple(replace(i, in_bcast=value) if i.port == port else i for i in snap.interfaces)
    return replace(snap, interfaces=ifs)


def test_record_poll_and_counter_history(store, snap_a):
    t0 = NOW
    t1 = NOW + timedelta(seconds=60)
    store.record_poll("sw-a", "10.0.0.5", t0, t0, snap_a, None)
    store.record_poll("sw-a", "10.0.0.5", t1, t1, replace_bcast(snap_a, "Gi1/0/14", 9000), None)
    hist = store.counter_history("sw-a", NOW - timedelta(hours=1))
    assert [v for _, v in hist["Gi1/0/14"]] == [5700, 10200]
    assert [t for t, _ in hist["Gi1/0/14"]] == [t0, t1]
    assert store.switches() == ["sw-a"]


def test_counter_history_respects_since(store, snap_a):
    old = NOW - timedelta(hours=2)
    store.record_poll("sw-a", "h", old, old, snap_a, None)
    store.record_poll("sw-a", "h", NOW, NOW, snap_a, None)
    hist = store.counter_history("sw-a", NOW - timedelta(hours=1))
    assert len(hist["Gi1/0/14"]) == 1


def test_failed_poll_is_recorded_without_snapshot(store):
    pid = store.record_poll("sw-a", "10.0.0.5", NOW, NOW, None, "timeout")
    assert pid > 0
    assert store.counter_history("sw-a", NOW - timedelta(hours=1)) == {}
    assert store.switches() == ["sw-a"]


def test_events_roundtrip_and_filter(store):
    evs = [
        Event(NOW - timedelta(minutes=3), "sw-a", "mac_flap", "high", "r1", port="Gi1/0/14", vlan=10, mac="aabb.cc00.0002"),
        Event(NOW - timedelta(minutes=2), "sw-core-1", "stp_topology_change", "medium", "r2", port="Gi1/0/1", vlan=10),
        Event(NOW - timedelta(minutes=1), "sw-a", "unreachable", "low", "r3"),
    ]
    store.write_events(evs)
    got = store.events(NOW - timedelta(minutes=10), NOW)
    assert got == evs
    assert store.events(NOW - timedelta(minutes=10), NOW, switch="sw-core-1") == [evs[1]]
    assert store.events(NOW - timedelta(minutes=2, seconds=30), NOW) == evs[1:]


def test_latest_stp_vlans_and_neighbors(store, snap_a):
    store.record_poll("sw-a", "h", NOW, NOW, snap_a, None)
    store.record_poll("sw-a", "h", NOW + timedelta(minutes=5), NOW + timedelta(minutes=5), None, "timeout")
    vlans = store.latest_stp_vlans("sw-a", NOW + timedelta(minutes=10))
    assert [v.vlan for v in vlans] == [1, 10] and vlans[1].tc_last_port == "Gi1/0/14"
    assert len(store.latest_neighbors("sw-a", NOW + timedelta(minutes=10))) == 3
    assert store.latest_stp_vlans("sw-a", NOW - timedelta(minutes=1)) == []


def test_prune_removes_old_snapshots_but_keeps_recent_events(store, snap_a):
    old = NOW - timedelta(days=15)
    ancient = NOW - timedelta(days=200)
    store.record_poll("sw-a", "h", old, old, snap_a, None)
    store.write_events([Event(old, "sw-a", "mac_flap", "high", "x"), Event(ancient, "sw-a", "mac_flap", "high", "y")])
    store.prune(NOW)
    assert store.counter_history("sw-a", NOW - timedelta(days=30)) == {}
    assert store.latest_stp_vlans("sw-a", NOW) == []
    assert len(store.events(NOW - timedelta(days=365), NOW)) == 1


def test_file_store_persists(tmp_path, snap_a):
    path = tmp_path / "db.sqlite"
    with Store(path) as s:
        s.init_schema()
        s.record_poll("sw-a", "h", NOW, NOW, snap_a, None)
    with Store(path) as s:
        s.init_schema()
        assert s.switches() == ["sw-a"]


def test_store_usable_from_worker_thread(store, snap_a):
    import concurrent.futures

    # The collector polls in asyncio.to_thread workers and prunes from the event loop thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.record_poll, "sw-a", "h", NOW, NOW, snap_a, None) for _ in range(4)]
        assert sorted(f.result() for f in futures) == [1, 2, 3, 4]
    assert len(store.counter_history("sw-a", NOW - timedelta(hours=1))["Gi1/0/14"]) == 4


def test_prune_uses_indexes_on_every_child_table(store):
    for table in ("interfaces", "mac_entries", "neighbors", "stp_vlans", "stp_ports", "log_lines"):
        plan = " ".join(row[3] for row in store.conn.execute(f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE poll_id = ?", (1,)))
        assert "SCAN" not in plan, f"{table}: {plan}"


def test_prune_removes_child_rows(store, snap_a):
    old = NOW - timedelta(days=15)
    store.record_poll("sw-a", "h", old, old, snap_a, None)
    store.prune(NOW)
    for table in ("polls", "interfaces", "mac_entries", "neighbors", "stp_vlans", "stp_ports", "log_lines"):
        assert store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_latest_snapshot_roundtrip(store, snap_a):
    assert store.latest_snapshot("sw-a") is None
    store.record_poll("sw-a", "h", NOW, NOW, snap_a, None)
    store.record_poll("sw-a", "h", NOW + timedelta(minutes=1), NOW + timedelta(minutes=1), None, "timeout")
    got = store.latest_snapshot("sw-a")
    assert got is not None and got.taken_at == NOW and got.switch == "sw-a"
    assert set(got.interfaces) == set(snap_a.interfaces) and set(got.macs) == set(snap_a.macs)
    assert set(got.neighbors) == set(snap_a.neighbors) and set(got.stp_vlans) == set(snap_a.stp_vlans)
    assert set(got.stp_ports) == set(snap_a.stp_ports) and set(got.log_lines) == set(snap_a.log_lines)
    assert got.stp_summary is None and got.parse_errors == ()


def test_record_poll_stores_only_new_log_lines_and_latest_snapshot_unions_them(store, snap_a):
    first, second = snap_a.log_lines[:2], snap_a.log_lines[2:]
    store.record_poll("sw-a", "h", NOW, NOW, snap_a, None, new_log_lines=first)
    store.record_poll("sw-a", "h", NOW + timedelta(minutes=1), NOW + timedelta(minutes=1), snap_a, None, new_log_lines=second)
    assert store.conn.execute("SELECT count(*) FROM log_lines").fetchone()[0] == len(snap_a.log_lines)
    got = store.latest_snapshot("sw-a")
    assert got is not None and set(got.log_lines) == set(snap_a.log_lines)
