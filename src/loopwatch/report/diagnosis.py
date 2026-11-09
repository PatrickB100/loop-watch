"""Which device started it: built from stored events, MAC tables, neighbours and DHCP hosts for
the top suspect port. Pure over the store's read methods; no network, no SSH."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loopwatch.model import Neighbor
from loopwatch.store import Store

if TYPE_CHECKING:
    from loopwatch.report.render import ReportData

BEFORE_WINDOW = timedelta(hours=1)  # how far back "on this port before the loop" looks


@dataclass(frozen=True)
class HostInfo:
    mac: str
    vendor: str
    hostname: str | None
    ip: str | None
    other_port: str | None  # for flapping MACs: the port it flapped against


@dataclass(frozen=True)
class Diagnosis:
    switch: str
    port: str
    first_event: datetime
    pattern: str  # bridge_on_edge | wireless_bridge | cable_between_ports | unknown
    headline: str
    connected: Neighbor | None
    attached: tuple[HostInfo, ...]
    flapping: tuple[HostInfo, ...]
    notes: tuple[str, ...]


def vendor_of(mac: str) -> str:
    """Organisation behind the MAC's OUI prefix, from netaddr's bundled IEEE registry."""
    from netaddr import EUI
    from netaddr.core import AddrFormatError, NotRegisteredError

    try:
        return str(EUI(mac).oui.registration().org)
    except (NotRegisteredError, AddrFormatError, IndexError, ValueError):
        return "unknown vendor"


def diagnose(store: Store, data: "ReportData", vendor: Callable[[str], str] = vendor_of) -> Diagnosis | None:
    if not data.suspects:
        return None
    top = data.suspects[0]
    switch, port = top.switch, top.port
    port_events = sorted((e for e in data.events if e.switch == switch and e.port == port), key=lambda e: e.time)
    first = port_events[0].time
    _, until = data.window

    connected = next((n for n in store.latest_neighbors(switch, until) if n.port == port), None)

    seen = store.mac_ports(switch, first - BEFORE_WINDOW, first)
    attached_macs = sorted(mac for mac, ports in seen.items() if ports == {port})

    flaps: dict[str, str | None] = {}
    for e in port_events:
        if e.kind == "mac_flap" and e.mac:
            flaps.setdefault(e.mac, e.other_port)
    hosts = store.hosts_for(set(attached_macs) | set(flaps))

    def info(mac: str, other: str | None) -> HostInfo:
        h = hosts.get(mac)
        return HostInfo(mac, vendor(mac), h.hostname if h else None, h.ip if h else None, other)

    attached = tuple(info(m, None) for m in attached_macs)
    flapping = tuple(info(m, flaps[m]) for m in sorted(flaps))
    other_ports = {o for o in flaps.values() if o}

    if any(e.kind == "unexpected_neighbor" for e in port_events):
        pattern, detail = "bridge_on_edge", ": a bridge appeared on this edge port"
    elif flapping and other_ports and all((switch, o) in data.ap_ports for o in other_ports):
        pattern, detail = "wireless_bridge", ": wireless-to-wired bridge on this port"
    elif flapping and len(other_ports) == 1 and connected is None and not attached:
        (other,) = other_ports
        pattern, detail = "cable_between_ports", f": cable or unmanaged device between {port} and {other}"
    else:
        pattern, detail = "unknown", ""
    headline = f"The loop most likely entered on {switch} {port}{detail}"

    notes: list[str] = []
    attached_names = {h.hostname for h in attached if h.hostname}
    for h in flapping:
        if h.hostname and h.hostname in attached_names:
            notes.append(f"{h.hostname} is on both sides: its Wi-Fi and wired adapters are bridged")
            break
    if connected is None and not attached:
        notes.append("No neighbour or MAC was learned on this port before the loop; the device is unknown.")
    if (attached or flapping) and not hosts:
        notes.append("No DHCP leases known; hostnames and IPs are unavailable.")
    if flapping:
        notes.append("Flapping MACs are seen through the loop and are usually victims, not the cause.")
    return Diagnosis(switch, port, first, pattern, headline, connected, attached, flapping, tuple(notes))
