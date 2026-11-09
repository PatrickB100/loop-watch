import re
from collections.abc import Mapping

from loopwatch.model import Interface
from loopwatch.ports import normalize_port

_HEADER = re.compile(
    r"^(\S+) is (up|down|administratively down), line protocol is (up|down)(?: \((\S+)\))?", re.M
)
# Front-panel and port-channel interfaces. AppGigabitEthernet (Ap) is the internal app-hosting port.
_PHYSICAL = re.compile(r"^(Gi|Te|Tw|Twe|Fo|Hu|Fa|Po)\d")
_IN_PKTS = re.compile(r"(\d+) packets input")
_OUT_PKTS = re.compile(r"(\d+) packets output")
_BCAST = re.compile(r"Received (\d+) broadcasts \((\d+) (?:IP )?multicasts?\)")
_IN_ERR = re.compile(r"(\d+) input errors")
_OUT_ERR = re.compile(r"(\d+) output errors")


def _int(pattern: re.Pattern[str], block: str, group: int = 1) -> int:
    m = pattern.search(block)
    return int(m.group(group)) if m else 0


def _link_from_header(admin: str, protocol: str, hint: str | None) -> str:
    if hint in ("connected", "notconnect", "disabled", "err-disabled"):
        return hint
    if admin == "administratively down":
        return "disabled"
    return "connected" if protocol == "up" else "notconnect"


def parse_interfaces(text: str, status: Mapping[str, tuple[str, str, str]]) -> tuple[Interface, ...]:
    """Counters per physical interface from `show interfaces`; link/vlan/speed from `show interfaces status`."""
    headers = list(_HEADER.finditer(text))
    result: list[Interface] = []
    for i, header in enumerate(headers):
        port = normalize_port(header.group(1))
        if not _PHYSICAL.match(port):
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[header.start():end]
        if port in status:
            link, vlan, speed = status[port]
        else:
            link, vlan, speed = _link_from_header(header.group(2), header.group(3), header.group(4)), "", ""
        result.append(
            Interface(
                port=port,
                link=link,
                vlan=vlan,
                speed=speed,
                in_pkts=_int(_IN_PKTS, block),
                out_pkts=_int(_OUT_PKTS, block),
                in_bcast=_int(_BCAST, block, 1),
                in_mcast=_int(_BCAST, block, 2),
                in_errors=_int(_IN_ERR, block),
                out_errors=_int(_OUT_ERR, block),
            )
        )
    return tuple(result)
