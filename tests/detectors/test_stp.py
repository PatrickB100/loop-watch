from loopwatch.detectors import stp
from loopwatch.detectors.context import DetectorContext
from tests.detectors.builders import make_event, make_snapshot, make_stp_port, make_stp_vlan


def ctx(prev, cur, recent=()):
    return DetectorContext(prev, cur, {}, list(recent), frozenset())


def test_topology_change_increment_emits_event():
    prev = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=12, tc_last_port="Gi1/0/14"),))
    cur = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=13, tc_last_port="Gi1/0/14"),))
    (ev,) = stp.detect(ctx(prev, cur))
    assert (ev.kind, ev.port, ev.vlan, ev.severity, ev.switch) == ("stp_topology_change", "Gi1/0/14", 10, "medium", "sw-a")
    assert ev.time == cur.taken_at and "12" in ev.reason and "13" in ev.reason


def test_topology_change_same_count_is_quiet():
    snap = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=12, tc_last_port="Gi1/0/14"),))
    assert stp.detect(ctx(snap, snap)) == []


def test_topology_change_escalates_after_three_in_five_minutes():
    prev = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=12),))
    cur = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=13, tc_last_port="Gi1/0/14"),))
    recent = [make_event("stp_topology_change", "Gi1/0/14", minutes_ago=m) for m in (1, 2, 3)]
    (ev,) = stp.detect(ctx(prev, cur, recent))
    assert ev.severity == "high"


def test_topology_change_two_recent_stays_medium():
    prev = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=12),))
    cur = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=13),))
    recent = [make_event("stp_topology_change", None, minutes_ago=m) for m in (1, 2)]
    recent += [make_event("stp_topology_change", None, minutes_ago=6)]  # outside the window
    (ev,) = stp.detect(ctx(prev, cur, recent))
    assert ev.severity == "medium" and ev.port is None


def test_no_prev_emits_nothing():
    cur = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=99),), stp_ports=(make_stp_port(10, "Gi1/0/1"),))
    assert stp.detect(ctx(None, cur)) == []


def test_role_change():
    prev = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/48", role="root", state="forwarding", port_type="network"),))
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/48", role="alternate", state="blocking", port_type="network"),))
    (ev,) = stp.detect(ctx(prev, cur))
    assert (ev.kind, ev.port, ev.vlan, ev.severity) == ("stp_role_change", "Gi1/0/48", 10, "medium")
    assert "root/forwarding" in ev.reason and "alternate/blocking" in ev.reason


def test_new_port_in_cur_is_not_a_role_change():
    prev = make_snapshot()
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/48"),))
    assert stp.detect(ctx(prev, cur)) == []
