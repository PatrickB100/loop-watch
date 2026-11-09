import re
from datetime import timedelta

from loopwatch.detectors.context import DetectorContext, new_log_lines
from loopwatch.model import Event
from loopwatch.ports import normalize_port

FLAP_WINDOW = timedelta(minutes=5)
FLAP_COUNT = 3

_LINK_IFACE = re.compile(r"Interface (\S+?),")
_ERR_DISABLE_IFACE = re.compile(r"putting (\S+) in err-disable", re.I)
_ERR_DISABLE_ON = re.compile(r"detected on (\S+?)[, ]")


def _link_flaps_from_log(ctx: DetectorContext) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in new_log_lines(ctx):
        if line.mnemonic != "LINK-3-UPDOWN":
            continue
        m = _LINK_IFACE.search(line.text)
        if m:
            port = normalize_port(m.group(1))
            counts[port] = counts.get(port, 0) + 1
    return counts


def detect(ctx: DetectorContext) -> list[Event]:
    events: list[Event] = []
    now = ctx.cur.taken_at
    recent_flaps: dict[str, int] = {}
    for e in ctx.recent_events:
        if e.kind == "link_flap" and e.port and e.time >= now - FLAP_WINDOW:
            recent_flaps[e.port] = recent_flaps.get(e.port, 0) + 1
    log_flaps = _link_flaps_from_log(ctx)
    reported: set[str] = set()

    if ctx.prev is not None:
        before = {i.port: i for i in ctx.prev.interfaces}
        for iface in ctx.cur.interfaces:
            old = before.get(iface.port)
            if old is None:
                continue
            if iface.link == "err-disabled" and old.link != "err-disabled":
                events.append(Event(now, ctx.cur.switch, "err_disable", "high",
                                    f"{iface.port} went {old.link} -> err-disabled", port=iface.port))
                continue
            flapped = {old.link, iface.link} == {"connected", "notconnect"}
            if flapped:
                repeated = recent_flaps.get(iface.port, 0) >= FLAP_COUNT or log_flaps.get(iface.port, 0) >= FLAP_COUNT
                events.append(Event(now, ctx.cur.switch, "link_flap", "medium" if repeated else "low",
                                    f"{iface.port} went {old.link} -> {iface.link}", port=iface.port))
                reported.add(iface.port)

    for port, count in log_flaps.items():
        if count >= FLAP_COUNT and port not in reported:
            events.append(Event(now, ctx.cur.switch, "link_flap", "medium",
                                f"{port} changed link state {count} times since last poll", port=port))

    for line in new_log_lines(ctx):
        if line.mnemonic != "PM-4-ERR_DISABLE":
            continue
        m = _ERR_DISABLE_IFACE.search(line.text) or _ERR_DISABLE_ON.search(line.text)
        events.append(Event(line.time or now, ctx.cur.switch, "err_disable", "high", line.text,
                            port=normalize_port(m.group(1)) if m else None))
    return events
