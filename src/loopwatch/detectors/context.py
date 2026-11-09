from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from loopwatch.model import Event, LogLine, Snapshot


@dataclass(frozen=True)
class DetectorContext:
    prev: Snapshot | None
    cur: Snapshot
    baseline_pps: Mapping[str, float]  # port -> rolling median broadcast+multicast pps
    recent_events: Sequence[Event]  # this switch's events from the last few minutes
    uplink_ports: frozenset[str]  # ports that face another managed switch


def new_log_lines(ctx: DetectorContext) -> tuple[LogLine, ...]:
    """Log lines in `cur` that were not in `prev`; all of them on the first poll."""
    if ctx.prev is None:
        return ctx.cur.log_lines
    seen = {(line.time, line.text) for line in ctx.prev.log_lines}
    return tuple(line for line in ctx.cur.log_lines if (line.time, line.text) not in seen)
