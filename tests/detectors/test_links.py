from loopwatch.detectors import links
from loopwatch.detectors.context import DetectorContext
from tests.detectors.builders import make_event, make_iface, make_log, make_snapshot


def ctx(prev, cur, recent=()):
    return DetectorContext(prev, cur, {}, list(recent), frozenset())


def test_link_flap_low_on_first_change():
    prev = make_snapshot(interfaces=(make_iface("Gi1/0/14", link="connected"),))
    cur = make_snapshot(interfaces=(make_iface("Gi1/0/14", link="notconnect"),))
    (ev,) = links.detect(ctx(prev, cur))
    assert (ev.kind, ev.port, ev.severity) == ("link_flap", "Gi1/0/14", "low")
    assert "connected" in ev.reason and "notconnect" in ev.reason


def test_link_flap_medium_after_three_recent():
    prev = make_snapshot(interfaces=(make_iface("Gi1/0/14", link="notconnect"),))
    cur = make_snapshot(interfaces=(make_iface("Gi1/0/14", link="connected"),))
    recent = [make_event("link_flap", "Gi1/0/14", minutes_ago=m, severity="low") for m in (1, 2, 4)]
    (ev,) = links.detect(ctx(prev, cur, recent))
    assert ev.severity == "medium"


def test_link_flap_from_repeated_log_lines():
    lines = tuple(make_log("LINK-3-UPDOWN", f"Interface GigabitEthernet1/0/14, changed state to {s}")
                  for s in ("down", "up", "down", "up"))
    cur = make_snapshot(log_lines=lines, interfaces=(make_iface("Gi1/0/14"),))
    (ev,) = links.detect(ctx(make_snapshot(interfaces=(make_iface("Gi1/0/14"),)), cur))
    assert (ev.kind, ev.port, ev.severity) == ("link_flap", "Gi1/0/14", "medium")


def test_two_log_lines_is_not_a_flap():
    lines = tuple(make_log("LINK-3-UPDOWN", f"Interface GigabitEthernet1/0/14, changed state to {s}") for s in ("down", "up"))
    cur = make_snapshot(log_lines=lines)
    assert links.detect(ctx(make_snapshot(), cur)) == []


def test_err_disable_from_status():
    prev = make_snapshot(interfaces=(make_iface("Gi1/0/7", link="connected"),))
    cur = make_snapshot(interfaces=(make_iface("Gi1/0/7", link="err-disabled"),))
    (ev,) = links.detect(ctx(prev, cur))
    assert (ev.kind, ev.port, ev.severity) == ("err_disable", "Gi1/0/7", "high")


def test_err_disable_still_disabled_is_quiet():
    snap = make_snapshot(interfaces=(make_iface("Gi1/0/7", link="err-disabled"),))
    assert links.detect(ctx(snap, snap)) == []


def test_err_disable_from_log():
    line = make_log("PM-4-ERR_DISABLE", "bpduguard error detected on Gi1/0/7, putting Gi1/0/7 in err-disable state")
    cur = make_snapshot(log_lines=(line,))
    (ev,) = links.detect(ctx(None, cur))
    assert (ev.kind, ev.port, ev.severity) == ("err_disable", "Gi1/0/7", "high")
    assert ev.reason == line.text
