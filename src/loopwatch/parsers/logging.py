"""Parse the `show logging` buffer.

Timezone names in IOS timestamps (CET, UTC, ...) are not resolved: the time is
read as written and tagged UTC. Events therefore never use LogLine.time as their
own time; they carry it in their reason text. Set switch clocks to UTC where you can.
"""

import re
from datetime import datetime, timedelta, timezone

from loopwatch.model import LogLine

# Anything before the %MNEMONIC is a prefix we try to read as a timestamp; the line is kept either way.
_LINE = re.compile(r"^(?P<prefix>.*?)(?P<text>%(?P<mnemonic>[A-Z0-9_]+-\d-[A-Z0-9_]+): .*)$")
# "Nov  9 09:41:58.102 CET", "Nov  9 2026 09:41:58", with optional sequence number and */. markers.
_STAMP = re.compile(
    r"(?<![A-Za-z])(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})(?: (?P<year>\d{4}))? (?P<hms>\d\d:\d\d:\d\d)(?:\.(?P<ms>\d+))?"
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


def _parse_time(prefix: str, now: datetime) -> datetime | None:
    m = _STAMP.search(prefix)
    if not m:
        return None  # uptime stamps (1w2d, 00:12:33) or no timestamp at all
    year = m.group("year") or str(now.year)
    try:
        stamp = datetime.strptime(f"{year} {m.group('mon')} {m.group('day')} {m.group('hms')}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None
    if m.group("ms"):
        stamp = stamp.replace(microsecond=int(m.group("ms").ljust(6, "0")[:6]))
    stamp = stamp.replace(tzinfo=timezone.utc)
    if not m.group("year") and stamp > now + timedelta(days=1):  # a December line read in January
        stamp = stamp.replace(year=now.year - 1)
    return stamp


def parse_logging(text: str, now: datetime) -> tuple[LogLine, ...]:
    lines: list[LogLine] = []
    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        lines.append(LogLine(_parse_time(m.group("prefix"), now), m.group("mnemonic"), m.group("text")))
    return tuple(lines)


def is_relevant(line: LogLine) -> bool:
    return bool(line.mnemonic) and line.mnemonic.startswith(RELEVANT_MNEMONICS)
