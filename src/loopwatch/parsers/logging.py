"""Parse the `show logging` buffer.

Timezone names in IOS timestamps (CET, UTC, ...) are not resolved: the time is
read as written and tagged UTC. Set switch clocks to UTC, or allow for the
offset when reading a report.
"""

import re
from datetime import datetime, timedelta, timezone

from loopwatch.model import LogLine

_LINE = re.compile(
    r"^(?:\d+: )?"  # sequence number
    r"(?:[*.])?"  # clock-not-authoritative / NTP-synced markers
    r"(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}) (?P<hms>\d\d:\d\d:\d\d)(?:\.(?P<ms>\d+))?(?: [A-Za-z]+)?: )?"
    r"(?P<text>%(?P<mnemonic>[A-Z0-9_]+-\d-[A-Z0-9_]+): .*)$"
)

RELEVANT_MNEMONICS: tuple[str, ...] = (
    "SW_MATM-4-MACFLAP_NOTIF",
    "SPANTREE-",
    "STORM_CONTROL-",
    "PM-4-ERR_DISABLE",
    "LINK-3-UPDOWN",
    "LINEPROTO-5-UPDOWN",
    "LINK-5-CHANGED",
)


def _parse_time(mon: str, day: str, hms: str, ms: str | None, now: datetime) -> datetime | None:
    try:
        stamp = datetime.strptime(f"{now.year} {mon} {day} {hms}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None
    if ms:
        stamp = stamp.replace(microsecond=int(ms.ljust(6, "0")[:6]))
    stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp > now + timedelta(days=1):  # a December line read in January
        stamp = stamp.replace(year=now.year - 1)
    return stamp


def parse_logging(text: str, now: datetime) -> tuple[LogLine, ...]:
    lines: list[LogLine] = []
    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        time = None
        if m.group("mon"):
            time = _parse_time(m.group("mon"), m.group("day"), m.group("hms"), m.group("ms"), now)
        lines.append(LogLine(time, m.group("mnemonic"), m.group("text")))
    return tuple(lines)


def is_relevant(line: LogLine) -> bool:
    return bool(line.mnemonic) and line.mnemonic.startswith(RELEVANT_MNEMONICS)
