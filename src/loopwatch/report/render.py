from collections.abc import Callable, Set
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, PackageLoader

from loopwatch.model import Event, LogLine
from loopwatch.report.diagnosis import Diagnosis, diagnose, vendor_of
from loopwatch.report.ranking import (
    ChainHop,
    Suspect,
    ap_ports_from_neighbors,
    rank_suspects,
    tc_chain,
    uplinks_from_neighbors,
)
from loopwatch.store import Store


@dataclass(frozen=True)
class ReportData:
    window: tuple[datetime, datetime]
    suspects: list[Suspect]
    events: list[Event]
    chains: dict[int, list[ChainHop]]
    log_lines: list[tuple[datetime, str, LogLine]] = field(default_factory=list)  # (polled at, device, line)
    uplinks: frozenset[tuple[str, str]] = frozenset()
    ap_ports: frozenset[tuple[str, str]] = frozenset()
    diagnosis: Diagnosis | None = None


def resolve_tz(name: str | None) -> tzinfo:
    """IANA zone name to tzinfo; empty means UTC. Unknown names are a configuration error."""
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}") from None


def build_report(
    store: Store,
    managed: Set[str],
    since: datetime,
    until: datetime,
    ap_ports: Set[tuple[str, str]] = frozenset(),
    vendor: Callable[[str], str] = vendor_of,
) -> ReportData:
    events = store.events(since, until)
    switches = store.switches()
    neighbors = {s: store.latest_neighbors(s, until) for s in switches}
    stp = {s: store.latest_stp_vlans(s, until) for s in switches}
    uplinks = frozenset(uplinks_from_neighbors(neighbors, managed))
    aps = frozenset(ap_ports_from_neighbors(neighbors) | set(ap_ports))
    suspects = rank_suspects(events, uplinks, aps)

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
    data = ReportData((since, until), suspects, events, chains, store.log_lines(since, until), uplinks, aps)
    return replace(data, diagnosis=diagnose(store, data, vendor))


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


def _fmt_time(t: datetime, tz: tzinfo = timezone.utc) -> str:
    return t.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _fmt_clock(t: datetime | None, tz: tzinfo = timezone.utc) -> str:
    return t.astimezone(tz).strftime("%H:%M:%S") if t else "-"


def render_text(data: ReportData, tz: tzinfo = timezone.utc) -> str:
    since, until = data.window
    out = [f"loop-watch report  {_fmt_time(since, tz)} .. {_fmt_time(until, tz)}", ""]
    d = data.diagnosis
    if d is not None:
        out.append("Diagnosis")
        out.append(f"  {d.headline} (first event {_fmt_clock(d.first_event, tz)})")
        if d.connected:
            caps = ",".join(sorted(d.connected.capabilities))
            out.append(f"  Connected device: {d.connected.name} ({d.connected.platform}, {caps})")
        for title, hosts in (("On this port before", d.attached), ("Flapped through it", d.flapping)):
            if hosts:
                out.append(f"  {title}:")
                for h in hosts:
                    other = f"  other port {h.other_port}" if h.other_port else ""
                    out.append(f"    {h.mac}  {h.vendor}  {h.hostname or '-'}  {h.ip or '-'}{other}")
        for note in d.notes:
            out.append(f"  Note: {note}")
        out.append("")
    out.append("Suspects")
    if not data.suspects:
        out.append("  (no events in this window)")
    for i, s in enumerate(data.suspects, 1):
        label = f" ({s.label})" if s.label else ""
        out.append(f"  {i:2d}. {s.switch} {s.port}  score {s.score}{label}  {_kinds_summary(s)}")
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
        fields = [_fmt_clock(e.time, tz), e.switch, e.kind, e.severity, e.port or "-",
                  str(e.vlan) if e.vlan is not None else "-", e.mac or "-", e.reason]
        out.append("  " + " ".join(fields))
    out.append("")
    out.append("Log")
    if not data.log_lines:
        out.append("  (no log lines)")
    for polled, device, line in data.log_lines:
        # the device's own stamp is shown as written (its clock, its zone), hence no tz conversion
        stamp = line.time.strftime("%H:%M:%S") if line.time else "-"
        out.append(f"  {_fmt_clock(polled, tz)} {device} {stamp} {line.text}")
    return "\n".join(out) + "\n"


def jinja_env(tz: tzinfo = timezone.utc, loader=None) -> Environment:
    env = Environment(loader=loader or PackageLoader("loopwatch.report", "templates"), autoescape=True)
    env.filters["chain"] = chain_text
    env.filters["kinds"] = _kinds_summary
    env.filters["fmt_time"] = lambda t: _fmt_time(t, tz)
    env.filters["clock"] = lambda t: _fmt_clock(t, tz)
    env.filters["stamp"] = lambda t: t.strftime("%H:%M:%S") if t else "-"
    return env


def render_html(data: ReportData, tz: tzinfo = timezone.utc) -> str:
    return jinja_env(tz).get_template("report.html.j2").render(data=data)
