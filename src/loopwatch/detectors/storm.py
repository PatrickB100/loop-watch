import re
import statistics
from collections.abc import Mapping
from datetime import datetime

from loopwatch.detectors.context import DetectorContext, log_reason, new_log_lines
from loopwatch.model import Event, Interface
from loopwatch.ports import normalize_port

# A port is in a storm when its broadcast+multicast rate is this many times its own
# one-hour median AND at least STORM_MIN_PPS, so a quiet port going from 1 to 30 pps stays quiet.
STORM_FACTOR = 20
STORM_MIN_PPS = 1000
# Unicast flooding (MAC table overflow, asymmetric paths) raises the total input rate without
# touching the broadcast counters. Same factor against the port's own median of all input
# packets, with a higher floor because busy ports legitimately run thousands of pps.
FLOOD_MIN_PPS = 5000

# "%STORM_CONTROL-3-FILTERED: A Broadcast storm detected on Gi1/0/14. A packet filter ..."
_STORM_CONTROL_PORT = re.compile(r"\bon (\S+?)\.")


def rate_pps(prev: Interface, cur: Interface, seconds: float) -> float | None:
    """Broadcast+multicast packets per second between two polls; None when unknowable."""
    return _rate((cur.in_bcast + cur.in_mcast) - (prev.in_bcast + prev.in_mcast), seconds)


def total_rate_pps(prev: Interface, cur: Interface, seconds: float) -> float | None:
    """All input packets per second between two polls; None when unknowable."""
    return _rate(cur.in_pkts - prev.in_pkts, seconds)


def _rate(delta: int, seconds: float) -> float | None:
    if seconds <= 0:
        return None
    if delta < 0:  # counters cleared or switch rebooted
        return None
    return delta / seconds


def baseline_from_history(history: Mapping[str, list[tuple[datetime, int]]]) -> dict[str, float]:
    """Per port, the median of the rates between consecutive samples. Needs at least two rates."""
    baseline: dict[str, float] = {}
    for port, samples in history.items():
        rates: list[float] = []
        for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
            seconds = (t1 - t0).total_seconds()
            if seconds > 0 and v1 >= v0:
                rates.append((v1 - v0) / seconds)
        if len(rates) >= 2:
            baseline[port] = statistics.median(rates)
    return baseline


def _vlan(iface: Interface) -> int | None:
    return int(iface.vlan) if iface.vlan.isdigit() else None


def detect(ctx: DetectorContext) -> list[Event]:
    events: list[Event] = []
    now = ctx.cur.taken_at
    if ctx.prev is not None:
        seconds = (now - ctx.prev.taken_at).total_seconds()
        before = {i.port: i for i in ctx.prev.interfaces}
        for iface in ctx.cur.interfaces:
            prev = before.get(iface.port)
            if prev is None:
                continue
            rate = rate_pps(prev, iface, seconds)
            baseline = ctx.baseline_pps.get(iface.port, 0.0)
            if rate is not None and rate >= STORM_MIN_PPS and rate >= STORM_FACTOR * baseline:
                events.append(Event(now, ctx.cur.switch, "broadcast_storm", "high",
                                    f"broadcast+multicast {rate:.0f} pps, baseline {baseline:.0f} pps",
                                    port=iface.port, vlan=_vlan(iface)))
                continue  # the storm explains the total rate too
            total = total_rate_pps(prev, iface, seconds)
            total_baseline = ctx.baseline_total_pps.get(iface.port, 0.0)
            if total is not None and total >= FLOOD_MIN_PPS and total >= STORM_FACTOR * total_baseline:
                events.append(Event(now, ctx.cur.switch, "unicast_flood", "high",
                                    f"all input {total:.0f} pps, baseline {total_baseline:.0f} pps, broadcasts normal",
                                    port=iface.port, vlan=_vlan(iface)))
    for line in new_log_lines(ctx):
        if not (line.mnemonic or "").startswith("STORM_CONTROL-"):
            continue
        m = _STORM_CONTROL_PORT.search(line.text)
        events.append(Event(now, ctx.cur.switch, "broadcast_storm", "high", log_reason(line),
                            port=normalize_port(m.group(1)) if m else None))
    return events
