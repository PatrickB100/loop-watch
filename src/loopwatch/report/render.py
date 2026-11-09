from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime

from jinja2 import Environment, PackageLoader

from loopwatch.model import Event
from loopwatch.report.ranking import ChainHop, Suspect, rank_suspects, tc_chain, uplinks_from_neighbors
from loopwatch.store import Store


@dataclass(frozen=True)
class ReportData:
    window: tuple[datetime, datetime]
    suspects: list[Suspect]
    events: list[Event]
    chains: dict[int, list[ChainHop]]


def build_report(store: Store, managed: Set[str], since: datetime, until: datetime) -> ReportData:
    events = store.events(since, until)
    switches = store.switches()
    neighbors = {s: store.latest_neighbors(s, until) for s in switches}
    stp = {s: store.latest_stp_vlans(s, until) for s in switches}
    suspects = rank_suspects(events, uplinks_from_neighbors(neighbors, managed))

    chains: dict[int, list[ChainHop]] = {}
    for vlan in sorted({e.vlan for e in events if e.kind == "stp_topology_change" and e.vlan is not None}):
        # Start wherever the chain runs longest: that is the switch furthest from the source,
        # so the path towards the edge is shown in full.
        best: list[ChainHop] = []
        for start in sorted(stp):
            hops = tc_chain(vlan, stp, neighbors, start)
            if len(hops) > len(best):
                best = hops
        if best:
            chains[vlan] = best
    return ReportData((since, until), suspects, events, chains)


def _kinds_summary(suspect: Suspect) -> str:
    counts: dict[str, int] = {}
    for e in suspect.events:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    return ", ".join(f"{k} x{n}" if n > 1 else k for k, n in sorted(counts.items()))


def chain_text(hops: list[ChainHop]) -> str:
    parts = [f"{h.switch} {h.port}" for h in hops]
    if hops and hops[-1].next_switch is None:
        parts.append("(unmanaged)")
    return " -> ".join(parts)


def _fmt_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def render_text(data: ReportData) -> str:
    since, until = data.window
    out = [f"loop-watch report  {_fmt_time(since)} .. {_fmt_time(until)}", ""]
    out.append("Suspects")
    if not data.suspects:
        out.append("  (no events in this window)")
    for i, s in enumerate(data.suspects, 1):
        out.append(f"  {i:2d}. {s.switch} {s.port}  score {s.score}  {_kinds_summary(s)}")
    out.append("")
    out.append("Topology change chain")
    if not data.chains:
        out.append("  (none)")
    for vlan, hops in data.chains.items():
        out.append(f"  VLAN {vlan}: {chain_text(hops)}")
    out.append("")
    out.append("Timeline")
    if not data.events:
        out.append("  (no events)")
    for e in data.events:
        fields = [e.time.strftime("%H:%M:%S"), e.switch, e.kind, e.severity, e.port or "-",
                  str(e.vlan) if e.vlan is not None else "-", e.mac or "-", e.reason]
        out.append("  " + " ".join(fields))
    return "\n".join(out) + "\n"


def render_html(data: ReportData) -> str:
    env = Environment(loader=PackageLoader("loopwatch.report", "templates"), autoescape=True)
    env.filters["chain"] = chain_text
    env.filters["kinds"] = _kinds_summary
    env.filters["fmt_time"] = _fmt_time
    return env.get_template("report.html.j2").render(data=data)
