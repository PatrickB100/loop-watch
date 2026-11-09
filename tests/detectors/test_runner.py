from loopwatch.detectors import edge, links, mac, runner, stp, storm
from loopwatch.detectors.context import DetectorContext
from loopwatch.detectors.runner import ALL_DETECTORS, run_detectors
from loopwatch.model import ParseError
from tests.detectors.builders import make_snapshot


def test_all_detectors_order():
    assert ALL_DETECTORS == (stp.detect, mac.detect, storm.detect, edge.detect, links.detect)


def test_run_detectors_adds_parse_error_events():
    cur = make_snapshot(parse_errors=(ParseError("show cdp neighbors detail", "boom"),))
    evs = run_detectors(DetectorContext(None, cur, {}, [], frozenset()))
    assert [(e.kind, e.severity) for e in evs] == [("parse_error", "low")]
    assert "show cdp neighbors detail" in evs[0].reason and "boom" in evs[0].reason


def test_run_detectors_runs_everything(snap_a):
    evs = run_detectors(DetectorContext(None, snap_a, {}, [], frozenset()))
    assert {e.kind for e in evs} == {"unexpected_neighbor", "mac_flap"}
