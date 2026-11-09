from ntc_templates.parse import parse_output

from loopwatch.model import MacEntry, Neighbor
from loopwatch.ports import normalize_mac, normalize_port


def _rows(command: str, text: str) -> list[dict[str, str]]:
    return parse_output(platform="cisco_ios", command=command, data=text)


def parse_mac_table(text: str) -> tuple[MacEntry, ...]:
    entries: list[MacEntry] = []
    for row in _rows("show mac address-table dynamic", text):
        ports = row.get("destination_port") or []
        if isinstance(ports, str):
            ports = [ports]
        for port in ports:
            entries.append(MacEntry(int(row["vlan_id"]), normalize_mac(row["destination_address"]), normalize_port(port)))
    return tuple(entries)


def parse_cdp_neighbors(text: str) -> tuple[Neighbor, ...]:
    return tuple(
        Neighbor(
            port=normalize_port(row["local_interface"]),
            name=row["neighbor_name"].strip(),
            remote_port=normalize_port(row["neighbor_interface"]),
            platform=row.get("platform", "").strip(),
            capabilities=frozenset(row.get("capabilities", "").split()),
        )
        for row in _rows("show cdp neighbors detail", text)
    )


def parse_lldp_neighbors(text: str) -> tuple[Neighbor, ...]:
    return tuple(
        Neighbor(
            port=normalize_port(row["local_interface"]),
            name=row["neighbor_name"].strip(),
            remote_port=normalize_port(row.get("neighbor_port_id", "")),
            platform=row.get("neighbor_description", "").strip(),
            capabilities=frozenset(c.strip() for c in row.get("capabilities", "").split(",") if c.strip()),
        )
        for row in _rows("show lldp neighbors detail", text)
    )


def parse_interfaces_status(text: str) -> dict[str, tuple[str, str, str]]:
    return {
        normalize_port(row["port"]): (row["status"].strip(), row["vlan_id"].strip(), row["speed"].strip())
        for row in _rows("show interfaces status", text)
    }
