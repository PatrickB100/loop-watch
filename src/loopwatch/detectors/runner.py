from collections.abc import Callable

from loopwatch.detectors import edge, links, mac, stp, storm
from loopwatch.detectors.context import DetectorContext
from loopwatch.model import Event

ALL_DETECTORS: tuple[Callable[[DetectorContext], list[Event]], ...] = (
    stp.detect,
    mac.detect,
    storm.detect,
    edge.detect,
    links.detect,
)


def run_detectors(ctx: DetectorContext) -> list[Event]:
    events: list[Event] = []
    for detect in ALL_DETECTORS:
        events.extend(detect(ctx))
    for err in ctx.cur.parse_errors:
        events.append(
            Event(ctx.cur.taken_at, ctx.cur.switch, "parse_error", "low", f"{err.command}: {err.message}")
        )
    return events
