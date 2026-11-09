from datetime import timedelta

from loopwatch.detectors.context import DetectorContext
from loopwatch.model import Event

# A fourth topology change within this window is no longer a routine link event.
ESCALATION_WINDOW = timedelta(minutes=5)
ESCALATION_COUNT = 3


def detect(ctx: DetectorContext) -> list[Event]:
    if ctx.prev is None:
        return []
    events: list[Event] = []
    now = ctx.cur.taken_at
    recent_tc = sum(
        1 for e in ctx.recent_events
        if e.kind == "stp_topology_change" and e.time >= now - ESCALATION_WINDOW
    )
    prev_vlans = {v.vlan: v for v in ctx.prev.stp_vlans}
    for vlan in ctx.cur.stp_vlans:
        before = prev_vlans.get(vlan.vlan)
        if before is None or vlan.tc_count <= before.tc_count:
            continue
        events.append(
            Event(
                time=now,
                switch=ctx.cur.switch,
                kind="stp_topology_change",
                severity="high" if recent_tc >= ESCALATION_COUNT else "medium",
                reason=f"topology changes {before.tc_count} -> {vlan.tc_count}, last from {vlan.tc_last_port or 'unknown'}",
                port=vlan.tc_last_port,
                vlan=vlan.vlan,
            )
        )

    prev_ports = {(p.vlan, p.port): p for p in ctx.prev.stp_ports}
    for port in ctx.cur.stp_ports:
        before = prev_ports.get((port.vlan, port.port))
        if before is None or (before.role, before.state) == (port.role, port.state):
            continue
        events.append(
            Event(
                time=now,
                switch=ctx.cur.switch,
                kind="stp_role_change",
                severity="medium",
                reason=f"{before.role}/{before.state} -> {port.role}/{port.state}",
                port=port.port,
                vlan=port.vlan,
            )
        )
    return events
