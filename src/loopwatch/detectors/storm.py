import statistics
from collections.abc import Mapping
from datetime import datetime

from loopwatch.detectors.context import DetectorContext
from loopwatch.model import Event, Interface

# A port is in a storm when its broadcast+multicast rate is this many times its own
# one-hour median AND at least STORM_MIN_PPS, so a quiet port going from 1 to 30 pps stays quiet.
STORM_FACTOR = 20
STORM_MIN_PPS = 1000


def rate_pps(prev: Interface, cur: Interface, seconds: float) -> float | None:
    """Broadcast+multicast packets per second between two polls; None when unknowable."""
    if seconds <= 0:
        return None
    delta = (cur.in_bcast + cur.in_mcast) - (prev.in_bcast + prev.in_mcast)
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


def detect(ctx: DetectorContext) -> list[Event]:
    if ctx.prev is None:
        return []
    seconds = (ctx.cur.taken_at - ctx.prev.taken_at).total_seconds()
    before = {i.port: i for i in ctx.prev.interfaces}
    events: list[Event] = []
    for iface in ctx.cur.interfaces:
        prev = before.get(iface.port)
        if prev is None:
            continue
        rate = rate_pps(prev, iface, seconds)
        if rate is None:
            continue
        baseline = ctx.baseline_pps.get(iface.port, 0.0)
        if rate >= STORM_MIN_PPS and rate >= STORM_FACTOR * baseline:
            events.append(
                Event(
                    time=ctx.cur.taken_at,
                    switch=ctx.cur.switch,
                    kind="broadcast_storm",
                    severity="high",
                    reason=f"broadcast+multicast {rate:.0f} pps, baseline {baseline:.0f} pps",
                    port=iface.port,
                    vlan=int(iface.vlan) if iface.vlan.isdigit() else None,
                )
            )
    return events
