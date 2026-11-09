from loopwatch.detectors.context import DetectorContext
from loopwatch.model import Event


def detect(ctx: DetectorContext) -> list[Event]:
    """A switch, or BPDUs, on a port configured as an edge (portfast) port."""
    events: list[Event] = []
    now = ctx.cur.taken_at
    edge_ports = {p.port for p in ctx.cur.stp_ports if p.port_type == "edge"}

    seen_before = set()
    if ctx.prev is not None:
        seen_before = {(n.port, n.name) for n in ctx.prev.neighbors if n.is_bridge}
    for n in ctx.cur.neighbors:
        if not n.is_bridge or n.port not in edge_ports or (n.port, n.name) in seen_before:
            continue
        events.append(
            Event(
                time=now,
                switch=ctx.cur.switch,
                kind="unexpected_neighbor",
                severity="high",
                reason=f"bridge {n.name} ({n.platform or 'unknown platform'}) appeared on edge port {n.port}",
                port=n.port,
            )
        )

    if ctx.prev is not None:
        before = {(p.vlan, p.port): p for p in ctx.prev.stp_ports}
        for p in ctx.cur.stp_ports:
            old = before.get((p.vlan, p.port))
            # IOS may drop operational portfast as soon as a BPDU arrives: the previous snapshot counts too.
            if old is None or "edge" not in (p.port_type, old.port_type) or p.bpdu_received <= old.bpdu_received:
                continue
            events.append(
                Event(
                    time=now,
                    switch=ctx.cur.switch,
                    kind="unexpected_neighbor",
                    severity="high",
                    reason=f"edge port {p.port} received BPDUs: {old.bpdu_received} -> {p.bpdu_received}",
                    port=p.port,
                    vlan=p.vlan,
                )
            )
    return events
