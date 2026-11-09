import re

from loopwatch.model import StpPort, StpSummary, StpVlan
from loopwatch.ports import normalize_port

_VLAN_HEADER = re.compile(r"^ ?VLAN(\d+) is executing", re.M)
_ROOT = re.compile(r"Current root has priority (\d+), address (\S+)")
_WE_ARE_ROOT = re.compile(r"We are the root of the spanning tree")
_BRIDGE_ID = re.compile(r"Bridge Identifier has priority (\d+), sysid (\d+), address (\S+)")
# "from <port>" is on the same line on some releases and on the next line on others.
_TC = re.compile(r"Number of topology changes (\d+) last change occurred (\S+) ago(?:\s+from (\S+))?")
_PORT_HEADER = re.compile(r"^ ?Port \d+ \((\S+)\) of VLAN\d+ is (\w+) (\w+)", re.M)
_PORTFAST = re.compile(r"The port is in the portfast")
_BPDU = re.compile(r"BPDU: sent (\d+), received (\d+)")

_AGE_UNITS = {"y": 31536000, "w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_age(text: str) -> int | None:
    """Convert IOS age strings (00:05:12, 1d02h, 3w2d, 2y5w) to seconds."""
    text = text.strip()
    if text.lower() == "never":
        return None
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts
        return h * 3600 + m * 60 + s
    total = 0
    for value, unit in re.findall(r"(\d+)([ywdhms])", text):
        total += int(value) * _AGE_UNITS[unit]
    return total


def parse_stp_detail(text: str) -> tuple[tuple[StpVlan, ...], tuple[StpPort, ...]]:
    vlans: list[StpVlan] = []
    ports: list[StpPort] = []
    headers = list(_VLAN_HEADER.finditer(text))
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[header.start():end]
        vlan = int(header.group(1))

        root = _ROOT.search(block)
        if root:
            root_id = f"{root.group(1)} {root.group(2)}"
        else:
            bridge = _BRIDGE_ID.search(block)
            root_id = f"{int(bridge.group(1)) + int(bridge.group(2))} {bridge.group(3)}" if bridge else ""
        tc = _TC.search(block)
        tc_count = int(tc.group(1)) if tc else 0
        tc_age = parse_age(tc.group(2)) if tc else None
        tc_port = normalize_port(tc.group(3)) if tc and tc.group(3) else None
        vlans.append(StpVlan(vlan, root_id, tc_count, tc_port, tc_age))

        port_headers = list(_PORT_HEADER.finditer(block))
        for j, ph in enumerate(port_headers):
            pend = port_headers[j + 1].start() if j + 1 < len(port_headers) else len(block)
            pblock = block[ph.start():pend]
            bpdu = _BPDU.search(pblock)
            ports.append(
                StpPort(
                    vlan=vlan,
                    port=normalize_port(ph.group(1)),
                    role=ph.group(2).lower(),
                    state=ph.group(3).lower(),
                    port_type="edge" if _PORTFAST.search(pblock) else "network",
                    bpdu_sent=int(bpdu.group(1)) if bpdu else 0,
                    bpdu_received=int(bpdu.group(2)) if bpdu else 0,
                )
            )
    return tuple(vlans), tuple(ports)


def _flag(text: str, pattern: str) -> bool:
    m = re.search(pattern + r"\s+is (enabled|disabled)", text, re.I | re.M)
    return bool(m and m.group(1).lower() == "enabled")


def parse_stp_summary(text: str) -> StpSummary:
    mode = re.search(r"Switch is in (\S+) mode", text)
    return StpSummary(
        mode=mode.group(1) if mode else "unknown",
        portfast_default=_flag(text, r"^Portfast Default"),
        bpduguard_default=_flag(text, r"Portfast(?: Edge)? BPDU Guard Default"),
        loopguard_default=_flag(text, r"Loopguard Default"),
    )
