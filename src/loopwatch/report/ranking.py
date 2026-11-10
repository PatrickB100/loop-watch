from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass

from loopwatch.model import Event, Neighbor, StpVlan

# Loops enter at edges; uplinks between managed switches and ports with an access point mostly
# relay the symptoms, so both score half.
UPLINK_DIVISOR = 2
MAX_SUSPECTS = 10
MAX_HOPS = 32


@dataclass(frozen=True)
class Suspect:
    switch: str
    port: str
    score: int
    events: tuple[Event, ...]
    label: str | None = None  # "uplink" | "ap" | None


@dataclass(frozen=True)
class ChainHop:
    switch: str
    port: str
    next_switch: str | None


def _score(events: Sequence[Event]) -> int:
    score = 0
    macs: set[str] = set()
    tc = 0
    for e in events:
        if e.kind in ("unexpected_neighbor", "err_disable"):
            score += 50
        elif e.kind == "mac_flap":
            macs.add(e.mac or e.reason)
        elif e.kind in ("broadcast_storm", "unicast_flood"):
            score += 30
        elif e.kind == "stp_topology_change":
            tc += 10
    score += min(20 * len(macs), 100)
    score += min(tc, 50)
    return score


def rank_suspects(
    events: Sequence[Event], uplinks: Set[tuple[str, str]], ap_ports: Set[tuple[str, str]] = frozenset()
) -> list[Suspect]:
    by_port: dict[tuple[str, str], list[Event]] = {}
    for e in events:
        if e.port:
            by_port.setdefault((e.switch, e.port), []).append(e)
    suspects: list[Suspect] = []
    for (switch, port), evs in by_port.items():
        score = _score(evs)
        label = "uplink" if (switch, port) in uplinks else "ap" if (switch, port) in ap_ports else None
        if label:
            score //= UPLINK_DIVISOR
        if score > 0:
            suspects.append(Suspect(switch, port, score, tuple(evs), label))
    suspects.sort(key=lambda s: (-s.score, s.switch, s.port))
    return suspects[:MAX_SUSPECTS]


def _managed_name(neighbor_name: str, managed: Set[str]) -> str | None:
    short = neighbor_name.split(".")[0]
    if neighbor_name in managed:
        return neighbor_name
    return short if short in managed else None


def uplinks_from_neighbors(
    neighbors_by_switch: Mapping[str, Sequence[Neighbor]], managed: Set[str]
) -> set[tuple[str, str]]:
    return {
        (switch, n.port)
        for switch, neighbors in neighbors_by_switch.items()
        for n in neighbors
        if _managed_name(n.name, managed) is not None
    }


def ap_ports_from_neighbors(neighbors_by_switch: Mapping[str, Sequence[Neighbor]]) -> set[tuple[str, str]]:
    """Ports whose CDP/LLDP neighbour advertises wireless-AP capability."""
    return {(switch, n.port) for switch, neighbors in neighbors_by_switch.items() for n in neighbors if "W" in n.capabilities}


def tc_chain(
    vlan: int,
    stp_by_switch: Mapping[str, Sequence[StpVlan]],
    neighbors_by_switch: Mapping[str, Sequence[Neighbor]],
    start: str,
) -> list[ChainHop]:
    """Follow 'last topology change from <port>' from switch to switch towards the source."""
    hops: list[ChainHop] = []
    visited: set[str] = set()
    switch = start
    managed = set(stp_by_switch)
    while switch not in visited and len(hops) < MAX_HOPS:
        visited.add(switch)
        entry = next((v for v in stp_by_switch.get(switch, ()) if v.vlan == vlan), None)
        if entry is None or not entry.tc_last_port:
            break
        neighbor = next((n for n in neighbors_by_switch.get(switch, ()) if n.port == entry.tc_last_port), None)
        next_switch = _managed_name(neighbor.name, managed) if neighbor else None
        hops.append(ChainHop(switch, entry.tc_last_port, next_switch))
        if next_switch is None:
            break
        switch = next_switch
    return hops
