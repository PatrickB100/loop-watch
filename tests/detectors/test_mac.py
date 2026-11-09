from loopwatch.detectors import mac
from loopwatch.detectors.context import DetectorContext, new_log_lines
from loopwatch.model import MacEntry
from tests.conftest import NOW
from tests.detectors.builders import make_log, make_snapshot


def ctx(prev, cur):
    return DetectorContext(prev, cur, {}, [], frozenset())


FLAP_TEXT = "Host aabb.cc00.0002 in vlan 10 is flapping between port Gi1/0/14 and port Gi1/0/48"


def test_mac_moves_between_ports():
    prev = make_snapshot(macs=(MacEntry(10, "aabb.cc00.0002", "Gi1/0/14"),))
    cur = make_snapshot(macs=(MacEntry(10, "aabb.cc00.0002", "Gi1/0/48"),))
    (ev,) = mac.detect(ctx(prev, cur))
    assert (ev.kind, ev.mac, ev.vlan, ev.port, ev.severity) == ("mac_flap", "aabb.cc00.0002", 10, "Gi1/0/48", "high")
    assert "Gi1/0/14" in ev.reason and "Gi1/0/48" in ev.reason


def test_mac_stays_put_is_quiet():
    snap = make_snapshot(macs=(MacEntry(10, "aabb.cc00.0002", "Gi1/0/14"),))
    assert mac.detect(ctx(snap, snap)) == []


def test_mac_in_two_vlans_is_not_a_flap():
    prev = make_snapshot(macs=(MacEntry(10, "aabb.cc00.0002", "Gi1/0/14"),))
    cur = make_snapshot(macs=(MacEntry(10, "aabb.cc00.0002", "Gi1/0/14"), MacEntry(20, "aabb.cc00.0002", "Gi1/0/48")))
    assert mac.detect(ctx(prev, cur)) == []


def test_macflap_log_line_emits_event_once():
    line = make_log("SW_MATM-4-MACFLAP_NOTIF", FLAP_TEXT)
    cur = make_snapshot(log_lines=(line,))
    (ev,) = mac.detect(ctx(make_snapshot(), cur))
    assert (ev.kind, ev.mac, ev.vlan, ev.port, ev.severity) == ("mac_flap", "aabb.cc00.0002", 10, "Gi1/0/14", "high")
    assert line.text in ev.reason and ev.time == cur.taken_at
    assert mac.detect(ctx(cur, cur)) == []


def test_macflap_log_line_with_long_port_names():
    text = "Host 0011.2233.4455 in vlan 20 is flapping between port GigabitEthernet1/0/3 and port Port-channel1"
    cur = make_snapshot(log_lines=(make_log("SW_MATM-4-MACFLAP_NOTIF", text),))
    (ev,) = mac.detect(ctx(None, cur))
    assert (ev.mac, ev.vlan, ev.port) == ("0011.2233.4455", 20, "Gi1/0/3")


def test_log_event_stamped_ahead_of_poll_uses_poll_time():
    # Switch clocks are read as written (CET read as UTC lands in the future); the event must still
    # fall inside a report window that ends at the poll time.
    from datetime import timedelta

    line = make_log("SW_MATM-4-MACFLAP_NOTIF", FLAP_TEXT, time=NOW + timedelta(hours=1))
    cur = make_snapshot(taken_at=NOW, log_lines=(line,))
    (ev,) = mac.detect(ctx(None, cur))
    assert ev.time == NOW and "11:00:00" in ev.reason


def test_new_log_lines_without_prev_returns_all():
    cur = make_snapshot(log_lines=(make_log("LINK-3-UPDOWN", "a"), make_log("LINK-3-UPDOWN", "b")))
    assert new_log_lines(DetectorContext(None, cur, {}, [], frozenset())) == cur.log_lines


def test_new_log_lines_excludes_seen():
    a, b = make_log("LINK-3-UPDOWN", "a"), make_log("LINK-3-UPDOWN", "b")
    prev = make_snapshot(log_lines=(a,))
    cur = make_snapshot(log_lines=(a, b))
    assert new_log_lines(DetectorContext(prev, cur, {}, [], frozenset())) == (b,)
