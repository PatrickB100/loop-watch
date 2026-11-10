from dataclasses import dataclass
from datetime import datetime

EVENT_KINDS: frozenset[str] = frozenset(
    {
        "stp_topology_change",
        "stp_role_change",
        "mac_flap",
        "broadcast_storm",
        "unicast_flood",
        "unexpected_neighbor",
        "link_flap",
        "err_disable",
        "unreachable",
        "parse_error",
    }
)

_BRIDGE_CAPABILITIES = frozenset({"S", "B", "Switch", "Bridge"})
# IP phones advertise B,T (LLDP) or "Host Phone Two-port Mac Relay" (CDP): a bridge in name only.
_PHONE_CAPABILITIES = frozenset({"T", "Phone", "Telephone"})


@dataclass(frozen=True)
class StpVlan:
    vlan: int
    root_id: str
    tc_count: int
    tc_last_port: str | None
    tc_last_age_s: int | None


@dataclass(frozen=True)
class StpPort:
    vlan: int
    port: str
    role: str  # root | designated | alternate | backup | disabled
    state: str  # forwarding | blocking | learning | listening | disabled
    port_type: str  # edge | network | normal
    bpdu_sent: int
    bpdu_received: int


@dataclass(frozen=True)
class StpSummary:
    mode: str
    portfast_default: bool
    bpduguard_default: bool
    loopguard_default: bool


@dataclass(frozen=True)
class Interface:
    port: str
    link: str  # connected | notconnect | disabled | err-disabled | unknown
    vlan: str
    speed: str
    in_pkts: int
    out_pkts: int
    in_bcast: int
    in_mcast: int
    in_errors: int
    out_errors: int


@dataclass(frozen=True)
class MacEntry:
    vlan: int
    mac: str
    port: str


@dataclass(frozen=True)
class Neighbor:
    port: str
    name: str
    remote_port: str
    platform: str
    capabilities: frozenset[str]

    @property
    def is_bridge(self) -> bool:
        if self.capabilities & _PHONE_CAPABILITIES:
            return False
        return bool(self.capabilities & _BRIDGE_CAPABILITIES)


@dataclass(frozen=True)
class LogLine:
    time: datetime | None
    mnemonic: str | None  # e.g. SW_MATM-4-MACFLAP_NOTIF, without the leading %
    text: str


@dataclass(frozen=True)
class Host:
    """A device known by MAC from a DHCP lease table."""

    mac: str
    ip: str | None
    hostname: str | None
    active: bool


@dataclass(frozen=True)
class ParseError:
    command: str
    message: str


@dataclass(frozen=True)
class Snapshot:
    switch: str
    taken_at: datetime
    stp_vlans: tuple[StpVlan, ...]
    stp_ports: tuple[StpPort, ...]
    stp_summary: StpSummary | None
    interfaces: tuple[Interface, ...]
    macs: tuple[MacEntry, ...]
    neighbors: tuple[Neighbor, ...]
    log_lines: tuple[LogLine, ...]
    parse_errors: tuple[ParseError, ...]
    hosts: tuple[Host, ...] = ()


@dataclass(frozen=True)
class Event:
    time: datetime
    switch: str
    kind: str
    severity: str  # low | medium | high
    reason: str
    port: str | None = None
    vlan: int | None = None
    mac: str | None = None
    other_port: str | None = None  # for mac_flap: the port the MAC was seen on before
