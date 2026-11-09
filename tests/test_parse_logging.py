from datetime import datetime, timezone

from loopwatch.model import LogLine
from loopwatch.parsers.logging import is_relevant, parse_logging

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


def test_parse_logging_keeps_mnemonic_lines(ios15_raw):
    lines = parse_logging(ios15_raw["show logging"], NOW)
    assert len(lines) == 5
    flap = next(line for line in lines if line.mnemonic == "SW_MATM-4-MACFLAP_NOTIF")
    assert flap.time == datetime(2026, 11, 9, 9, 42, 10, 500000, tzinfo=timezone.utc)
    assert "aabb.cc00.0002" in flap.text and "%SW_MATM" in flap.text


def test_parse_logging_timestamp_without_milliseconds(ios15_raw):
    trap = next(line for line in parse_logging(ios15_raw["show logging"], NOW) if line.mnemonic == "SPANTREE-5-TOPOTRAP")
    assert trap.time == datetime(2026, 11, 9, 9, 42, 11, tzinfo=timezone.utc)


def test_parse_logging_line_without_timestamp(ios15_raw):
    cfg = next(line for line in parse_logging(ios15_raw["show logging"], NOW) if line.mnemonic == "SYS-5-CONFIG_I")
    assert cfg.time is None


def test_parse_logging_year_rollover():
    text = "000001: *Dec 31 23:59:00: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to down"
    (line,) = parse_logging(text, datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc))
    assert line.time.year == 2026


def test_parse_logging_ignores_header_lines(ios15_raw):
    assert all(line.mnemonic for line in parse_logging(ios15_raw["show logging"], NOW))


def test_is_relevant():
    assert is_relevant(LogLine(None, "SW_MATM-4-MACFLAP_NOTIF", ""))
    assert is_relevant(LogLine(None, "SPANTREE-5-TOPOTRAP", ""))
    assert is_relevant(LogLine(None, "PM-4-ERR_DISABLE", ""))
    assert is_relevant(LogLine(None, "STORM_CONTROL-3-SHUTDOWN", ""))
    assert not is_relevant(LogLine(None, "SYS-5-CONFIG_I", ""))
    assert not is_relevant(LogLine(None, None, "garbage"))
