from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TypeVar

from loopwatch.model import ParseError, Snapshot
from loopwatch.parsers.interfaces import parse_interfaces
from loopwatch.parsers.logging import is_relevant, parse_logging
from loopwatch.parsers.stp import parse_stp_detail, parse_stp_summary
from loopwatch.parsers.tables import (
    parse_cdp_neighbors,
    parse_interfaces_status,
    parse_lldp_neighbors,
    parse_mac_table,
)

T = TypeVar("T")

_CLI_ERRORS = ("% Invalid input", "% Incomplete command", "% Ambiguous command")


def _section(
    raw: Mapping[str, str],
    command: str,
    parse: Callable[[str], T],
    empty: T,
    errors: list[ParseError],
    allow_empty: bool = False,
) -> T:
    """Parse one command's output; any problem becomes a ParseError and `empty` is returned.
    `allow_empty` is for tables that are legitimately empty (no neighbours, no leases)."""
    text = raw.get(command)
    if text is None:  # skipped in the inventory
        return empty
    if not text.strip():
        if not allow_empty:
            errors.append(ParseError(command, "empty output"))
        return empty
    if any(marker in text for marker in _CLI_ERRORS):
        errors.append(ParseError(command, text.strip()[:200]))
        return empty
    try:
        return parse(text)
    except Exception as exc:  # parsers must never take the whole snapshot down
        errors.append(ParseError(command, f"{type(exc).__name__}: {exc}"[:200]))
        return empty


def parse_snapshot(switch: str, raw: Mapping[str, str], taken_at: datetime) -> Snapshot:
    errors: list[ParseError] = []
    stp_vlans, stp_ports = _section(raw, "show spanning-tree detail", parse_stp_detail, ((), ()), errors)
    stp_summary = _section(raw, "show spanning-tree summary", parse_stp_summary, None, errors)
    status = _section(raw, "show interfaces status", parse_interfaces_status, {}, errors)
    interfaces = _section(raw, "show interfaces", lambda t: parse_interfaces(t, status), (), errors)
    macs = _section(raw, "show mac address-table dynamic", parse_mac_table, (), errors)
    cdp = _section(raw, "show cdp neighbors detail", parse_cdp_neighbors, (), errors)
    lldp = _section(raw, "show lldp neighbors detail", parse_lldp_neighbors, (), errors)
    log_lines = _section(raw, "show logging", lambda t: parse_logging(t, taken_at), (), errors)
    return Snapshot(
        switch=switch,
        taken_at=taken_at,
        stp_vlans=tuple(stp_vlans),
        stp_ports=tuple(stp_ports),
        stp_summary=stp_summary,
        interfaces=tuple(interfaces),
        macs=tuple(macs),
        neighbors=tuple(cdp) + tuple(lldp),
        log_lines=tuple(line for line in log_lines if is_relevant(line)),
        parse_errors=tuple(errors),
    )
