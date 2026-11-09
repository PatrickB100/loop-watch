"""End to end: someone plugs a desk switch into Gi1/0/14 and the office loops."""

from datetime import timedelta

from loopwatch.collector import poll_once
from loopwatch.report.render import build_report, render_text
from loopwatch.store import Store
from loopwatch.transport import FakeTransport
from tests.conftest import FIXTURES, NOW

SCENARIO = FIXTURES / "scenario"


def run_scenario(inv):
    store = Store(":memory:")
    store.init_schema()
    prev = None
    per_step = []
    for i, t in enumerate(("t0", "t1", "t2")):
        prev, events = poll_once(inv.by_name("sw-a"), inv, FakeTransport(SCENARIO / t), store, prev,
                                 NOW + timedelta(seconds=60 * i))
        per_step.append(events)
    return store, per_step


def test_desk_switch_scenario_ranks_gi14_first(inv):
    store, steps = run_scenario(inv)
    assert steps[0] == []  # quiet baseline
    assert {e.kind for e in steps[1]} == {"unexpected_neighbor", "stp_topology_change"}
    assert {e.kind for e in steps[2]} == {"mac_flap", "broadcast_storm"}
    data = build_report(store, inv.names, NOW, NOW + timedelta(minutes=5))
    kinds = {e.kind for e in data.events}
    assert {"unexpected_neighbor", "stp_topology_change", "mac_flap", "broadcast_storm"} <= kinds
    assert (data.suspects[0].switch, data.suspects[0].port) == ("sw-a", "Gi1/0/14")
    assert data.chains == {10: data.chains[10]} and data.chains[10][-1].port == "Gi1/0/14"


def test_desk_switch_scenario_report_reads_well(inv):
    store, _ = run_scenario(inv)
    text = render_text(build_report(store, inv.names, NOW, NOW + timedelta(minutes=5)))
    assert "1. sw-a Gi1/0/14" in text
    assert "unmanaged-sw" in text and "broadcast+multicast 10000 pps" in text
    assert "VLAN 10: sw-a Gi1/0/14 -> (unmanaged)" in text
