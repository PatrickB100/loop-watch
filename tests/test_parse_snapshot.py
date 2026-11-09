from loopwatch.parsers.logging import is_relevant
from loopwatch.parsers.snapshot import parse_snapshot
from tests.conftest import NOW


def test_parse_snapshot_full(ios15_raw):
    s = parse_snapshot("sw-a", ios15_raw, NOW)
    assert s.switch == "sw-a" and s.taken_at == NOW
    assert s.parse_errors == ()
    assert len(s.stp_vlans) == 2 and len(s.stp_ports) == 4
    assert s.stp_summary is not None and s.stp_summary.mode == "rapid-pvst"
    assert len(s.interfaces) == 3 and len(s.macs) == 42 and len(s.neighbors) == 3
    assert all(is_relevant(line) for line in s.log_lines) and len(s.log_lines) == 4


def test_parse_snapshot_invalid_input_becomes_parse_error(ios15_raw):
    raw = dict(ios15_raw, **{"show cdp neighbors detail": "% Invalid input detected at '^' marker."})
    s = parse_snapshot("sw-a", raw, NOW)
    assert [e.command for e in s.parse_errors] == ["show cdp neighbors detail"]
    assert "Invalid input" in s.parse_errors[0].message
    assert len(s.neighbors) == 1 and len(s.macs) > 0


def test_parse_snapshot_empty_output_becomes_parse_error(ios15_raw):
    raw = dict(ios15_raw, **{"show spanning-tree summary": "   \n"})
    s = parse_snapshot("sw-a", raw, NOW)
    assert [e.command for e in s.parse_errors] == ["show spanning-tree summary"]
    assert s.stp_summary is None and len(s.stp_vlans) == 2


def test_parse_snapshot_exception_is_isolated(ios15_raw):
    raw = dict(ios15_raw, **{"show mac address-table dynamic": "Vlan Mac Address Type Ports\n  x   not-a-mac  DYNAMIC Gi1/0/1\n"})
    s = parse_snapshot("sw-a", raw, NOW)
    assert [e.command for e in s.parse_errors] == ["show mac address-table dynamic"]
    assert len(s.parse_errors[0].message) <= 200 and len(s.interfaces) == 3


def test_parse_snapshot_skipped_command_is_not_an_error(ios15_raw):
    raw = {k: v for k, v in ios15_raw.items() if k != "show cdp neighbors detail"}
    s = parse_snapshot("sw-a", raw, NOW)
    assert s.parse_errors == () and len(s.neighbors) == 1
