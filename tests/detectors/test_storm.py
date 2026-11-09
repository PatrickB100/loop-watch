from datetime import timedelta

from loopwatch.detectors.context import DetectorContext
from loopwatch.detectors.storm import (
    FLOOD_MIN_PPS,
    STORM_FACTOR,
    STORM_MIN_PPS,
    baseline_from_history,
    detect,
    rate_pps,
    total_rate_pps,
)
from tests.conftest import NOW
from tests.detectors.builders import make_iface, make_log, make_snapshot


def ctx(prev, cur, baseline, total_baseline=None):
    return DetectorContext(prev, cur, baseline, [], frozenset(), baseline_total_pps=total_baseline or {})


def two_polls(prev_total, cur_total, seconds=10):
    prev = make_snapshot(taken_at=NOW, interfaces=(make_iface("Gi1/0/14", in_bcast=prev_total, in_mcast=0),))
    cur = make_snapshot(taken_at=NOW + timedelta(seconds=seconds),
                        interfaces=(make_iface("Gi1/0/14", in_bcast=cur_total, in_mcast=0),))
    return prev, cur


def test_constants():
    assert STORM_FACTOR == 20 and STORM_MIN_PPS == 1000


def test_rate_pps_counts_broadcast_and_multicast():
    assert rate_pps(make_iface("Gi1/0/14"), make_iface("Gi1/0/14", in_bcast=300, in_mcast=300), 60) == 10.0


def test_rate_none_when_counter_resets():
    assert rate_pps(make_iface("Gi1/0/14", in_bcast=9000), make_iface("Gi1/0/14", in_bcast=10), 60) is None


def test_rate_none_when_no_time_passed():
    assert rate_pps(make_iface("Gi1/0/14"), make_iface("Gi1/0/14", in_bcast=10), 0) is None


def test_storm_fires_over_factor_and_minimum():
    prev = make_snapshot(taken_at=NOW, interfaces=(make_iface("Gi1/0/14", in_bcast=0, in_mcast=0),))
    cur = make_snapshot(taken_at=NOW + timedelta(seconds=10),
                        interfaces=(make_iface("Gi1/0/14", in_bcast=90000, in_mcast=10000),))
    (ev,) = detect(ctx(prev, cur, {"Gi1/0/14": 50.0}))
    assert (ev.kind, ev.port, ev.severity, ev.switch) == ("broadcast_storm", "Gi1/0/14", "high", "sw-a")
    assert "10000" in ev.reason and "50" in ev.reason and ev.time == cur.taken_at


def test_no_storm_below_minimum_even_if_factor_exceeded():
    prev, cur = two_polls(0, 5000)  # 500 pps against baseline 10
    assert detect(ctx(prev, cur, {"Gi1/0/14": 10.0})) == []


def test_no_storm_when_within_factor():
    prev, cur = two_polls(0, 50000)  # 5000 pps against baseline 1000
    assert detect(ctx(prev, cur, {"Gi1/0/14": 1000.0})) == []


def test_storm_without_baseline_uses_minimum_only():
    prev, cur = two_polls(0, 20000)  # 2000 pps, no history
    (ev,) = detect(ctx(prev, cur, {}))
    assert ev.kind == "broadcast_storm"


def test_counter_reset_does_not_fire():
    prev, cur = two_polls(1_000_000, 100)
    assert detect(ctx(prev, cur, {})) == []


def test_no_prev_is_quiet():
    cur = make_snapshot(interfaces=(make_iface("Gi1/0/14", in_bcast=10**9),))
    assert detect(ctx(None, cur, {})) == []


def test_baseline_from_history_is_median_of_rates():
    h = {"Gi1/0/1": [(NOW, 0), (NOW + timedelta(seconds=60), 600), (NOW + timedelta(seconds=120), 1800),
                     (NOW + timedelta(seconds=180), 2400)]}
    assert baseline_from_history(h) == {"Gi1/0/1": 10.0}  # rates 10, 20, 10 -> median 10


def test_baseline_skips_resets_and_short_history():
    h = {"Gi1/0/1": [(NOW, 5000), (NOW + timedelta(seconds=60), 10)],  # reset only -> no rate
         "Gi1/0/2": [(NOW, 0), (NOW + timedelta(seconds=60), 600)]}  # one rate -> too short
    assert baseline_from_history(h) == {}


def test_flood_constant():
    assert FLOOD_MIN_PPS == 5000


def test_total_rate_pps_counts_all_input_packets():
    assert total_rate_pps(make_iface("Gi1/0/14", in_pkts=0), make_iface("Gi1/0/14", in_pkts=6000), 60) == 100.0
    assert total_rate_pps(make_iface("Gi1/0/14", in_pkts=9000), make_iface("Gi1/0/14", in_pkts=10), 60) is None


def test_unicast_flood_fires_on_total_rate_without_broadcasts():
    prev = make_snapshot(taken_at=NOW, interfaces=(make_iface("Gi1/0/14", in_pkts=1_000_000),))
    cur = make_snapshot(taken_at=NOW + timedelta(seconds=10),
                        interfaces=(make_iface("Gi1/0/14", in_pkts=1_000_000 + 600_000),))  # 60k pps, no bcast
    (ev,) = detect(ctx(prev, cur, {}, {"Gi1/0/14": 500.0}))
    assert ev.kind == "unicast_flood" and ev.severity == "high" and ev.port == "Gi1/0/14"
    assert "60000 pps" in ev.reason and "baseline 500" in ev.reason


def test_unicast_flood_needs_factor_and_minimum():
    prev = make_snapshot(taken_at=NOW, interfaces=(make_iface("Gi1/0/14", in_pkts=0),))
    busy = make_snapshot(taken_at=NOW + timedelta(seconds=10), interfaces=(make_iface("Gi1/0/14", in_pkts=600_000),))
    assert detect(ctx(prev, busy, {}, {"Gi1/0/14": 50_000.0})) == []  # a busy server port, only 1.2x its baseline
    small = make_snapshot(taken_at=NOW + timedelta(seconds=10), interfaces=(make_iface("Gi1/0/14", in_pkts=40_000),))
    assert detect(ctx(prev, small, {}, {"Gi1/0/14": 10.0})) == []  # 4000 pps: under FLOOD_MIN_PPS


def test_broadcast_storm_is_not_also_reported_as_unicast_flood():
    prev = make_snapshot(taken_at=NOW, interfaces=(make_iface("Gi1/0/14", in_pkts=0, in_bcast=0),))
    cur = make_snapshot(taken_at=NOW + timedelta(seconds=10),
                        interfaces=(make_iface("Gi1/0/14", in_pkts=600_000, in_bcast=600_000),))
    events = detect(ctx(prev, cur, {"Gi1/0/14": 10.0}, {"Gi1/0/14": 10.0}))
    assert [e.kind for e in events] == ["broadcast_storm"]


def test_storm_control_log_lines_become_storm_events():
    prev = make_snapshot(taken_at=NOW)
    cur = make_snapshot(taken_at=NOW + timedelta(seconds=10), log_lines=(
        make_log("STORM_CONTROL-3-FILTERED", "A Broadcast storm detected on Gi1/0/14. A packet filter action has been applied on the interface."),
        make_log("STORM_CONTROL-3-SHUTDOWN", "A packet storm was detected on GigabitEthernet1/0/7. The interface has been disabled."),
    ))
    events = detect(ctx(prev, cur, {}))
    assert [(e.kind, e.port, e.severity) for e in events] == [
        ("broadcast_storm", "Gi1/0/14", "high"), ("broadcast_storm", "Gi1/0/7", "high")]
    assert "STORM_CONTROL-3-SHUTDOWN" in events[1].reason
