"""Parsers for MikroTik RouterOS `print` output.

Layouts are written from RouterOS 7 documentation, not recorded from a device; the
parse-error path covers what differs until a real capture replaces the fixtures.
Log stamps are read as written and tagged UTC, like the Cisco parser does.
"""

import re
from collections.abc import Mapping
from datetime import datetime, timezone

from loopwatch.model import Host, Interface, LogLine, Neighbor, Snapshot
from loopwatch.parsers.snapshot import _section
from loopwatch.ports import normalize_mac, normalize_port

MIKROTIK_COMMANDS: tuple[str, ...] = (
    "/interface print without-paging",
    "/interface ethernet print stats without-paging",
    "/ip neighbor print detail without-paging",
    "/log print without-paging",
    "/ip dhcp-server lease print detail without-paging",
)

RELEVANT_TOPICS = frozenset({"interface", "bridge", "stp", "dhcp", "error", "critical"})

_TABLE_ROW = re.compile(r"^\s*(\d+)\s+(?:([DXRS]{1,4})\s+)?(\S+)\s+(\S+)")
_ENTRY_START = re.compile(r"^\s*\d+\s")
_TOKEN = re.compile(r'([a-z0-9-]+)=("([^"]*)"|\S*)')
_LOG = re.compile(
    r"^\s*(?:(?P<mon>[a-z]{3})/(?P<day>\d\d)(?:/(?P<year>\d{4}))? )?(?P<hms>\d\d:\d\d:\d\d) "
    r"(?P<topics>[a-z][a-z,-]*) (?P<msg>.*)$"
)
_LINK_LINE = re.compile(r"^\S+ link (?:up|down)")
_RX_ERROR = re.compile(r"^rx-.*(error|fcs|align|overflow|too-short|too-long)")
_TX_ERROR = re.compile(r"^tx-.*(error|collision|underrun)")

# RouterOS system-caps tokens -> the letters the Neighbor model understands.
_CAPS = {"bridge": "B", "switch": "S", "router": "R", "telephone": "T", "wlan-ap": "W", "station": "H"}


def parse_interface_list(text: str) -> dict[str, str]:
    """name -> link state for interfaces of type `ether` (physical ports, including SFP)."""
    links: dict[str, str] = {}
    for line in text.splitlines():
        m = _TABLE_ROW.match(line)
        if not m or m.group(4) != "ether":
            continue
        flags = m.group(2) or ""
        links[m.group(3)] = "disabled" if "X" in flags else "connected" if "R" in flags else "notconnect"
    return links


def parse_ethernet_stats(text: str) -> dict[str, dict[str, int]]:
    """`print stats` lays properties out as rows with one column per interface, in blocks that
    each start with a `name:` row. Returns name -> {property: value}."""
    stats: dict[str, dict[str, int]] = {}
    names: list[str] = []
    for line in text.splitlines():
        if ":" not in line:
            names = []
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        values = rest.split()
        if key == "name":
            names = values
            for n in names:
                stats.setdefault(n, {})
            continue
        if not names:
            continue
        for name, value in zip(names, values):
            try:
                stats[name][key] = int(value)
            except ValueError:
                continue
    return stats


def _entries(text: str) -> list[dict[str, str]]:
    """`print detail` output: numbered entries of key=value tokens, wrapped over indented lines."""
    entries: list[dict[str, str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if _ENTRY_START.match(line):
            if current:
                entries.append(_tokens(" ".join(current)))
            current = [line]
        elif line.strip() and current:
            current.append(line)
    if current:
        entries.append(_tokens(" ".join(current)))
    return entries


def _tokens(text: str) -> dict[str, str]:
    return {m.group(1): m.group(3) if m.group(3) is not None else m.group(2) for m in _TOKEN.finditer(text)}


def parse_ip_neighbors(text: str) -> tuple[Neighbor, ...]:
    out: list[Neighbor] = []
    for e in _entries(text):
        if "interface" not in e:
            continue
        caps = frozenset(_CAPS.get(c, c) for c in e.get("system-caps", "").split(",") if c)
        platform = " ".join(p for p in (e.get("platform"), e.get("board")) if p)
        out.append(
            Neighbor(
                port=e["interface"],
                name=e.get("identity", ""),
                remote_port=normalize_port(e.get("interface-name", "")),
                platform=platform,
                capabilities=caps,
            )
        )
    return tuple(out)


def _log_time(m: re.Match[str], now: datetime) -> datetime | None:
    hms = m.group("hms")
    try:
        if m.group("mon") is None:
            return datetime.combine(now.date(), datetime.strptime(hms, "%H:%M:%S").time(), tzinfo=timezone.utc)
        year = m.group("year") or str(now.year)
        stamp = datetime.strptime(f"{year} {m.group('mon')} {m.group('day')} {hms}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None
    stamp = stamp.replace(tzinfo=timezone.utc)
    if not m.group("year") and stamp > now.replace(tzinfo=timezone.utc) and (stamp - now).days > 1:
        stamp = stamp.replace(year=now.year - 1)  # a December line read in January
    return stamp


def parse_log(text: str, now: datetime) -> tuple[LogLine, ...]:
    lines: list[LogLine] = []
    for raw in text.splitlines():
        m = _LOG.match(raw)  # RouterOS prints months and topics in lowercase
        if not m:
            continue
        topics, msg = m.group("topics"), m.group("msg")
        if not RELEVANT_TOPICS & set(topics.split(",")):
            continue
        mnemonic = "LINK-3-UPDOWN" if _LINK_LINE.match(msg) else topics
        lines.append(LogLine(_log_time(m, now), mnemonic, msg))
    return tuple(lines)


def parse_dhcp_leases(text: str) -> tuple[Host, ...]:
    hosts: list[Host] = []
    for e in _entries(text):
        mac = e.get("mac-address")
        if not mac:
            continue
        hosts.append(Host(normalize_mac(mac), e.get("address") or None, e.get("host-name") or None,
                          e.get("status") == "bound"))
    return tuple(hosts)


def _interfaces(links: Mapping[str, str], stats: Mapping[str, Mapping[str, int]]) -> tuple[Interface, ...]:
    out: list[Interface] = []
    for name in sorted(set(links) | set(stats)):
        s = stats.get(name, {})
        out.append(
            Interface(
                port=name,
                link=links.get(name, "unknown"),
                vlan="",
                speed="",
                in_pkts=s.get("rx-packet", 0),
                out_pkts=s.get("tx-packet", 0),
                in_bcast=s.get("rx-broadcast", 0),
                in_mcast=s.get("rx-multicast", 0),
                in_errors=sum(v for k, v in s.items() if _RX_ERROR.match(k)),
                out_errors=sum(v for k, v in s.items() if _TX_ERROR.match(k)),
            )
        )
    return tuple(out)


def parse_mikrotik_snapshot(switch: str, raw: Mapping[str, str], taken_at: datetime) -> Snapshot:
    errors: list = []
    links = _section(raw, MIKROTIK_COMMANDS[0], parse_interface_list, {}, errors)
    stats = _section(raw, MIKROTIK_COMMANDS[1], parse_ethernet_stats, {}, errors)
    neighbors = _section(raw, MIKROTIK_COMMANDS[2], parse_ip_neighbors, (), errors, allow_empty=True)
    log_lines = _section(raw, MIKROTIK_COMMANDS[3], lambda t: parse_log(t, taken_at), (), errors)
    hosts = _section(raw, MIKROTIK_COMMANDS[4], parse_dhcp_leases, (), errors, allow_empty=True)
    return Snapshot(
        switch=switch,
        taken_at=taken_at,
        stp_vlans=(),
        stp_ports=(),
        stp_summary=None,
        interfaces=_interfaces(links, stats),
        macs=(),
        neighbors=tuple(neighbors),
        log_lines=tuple(log_lines),
        parse_errors=tuple(errors),
        hosts=tuple(hosts),
    )
