from loopwatch.model import MacEntry
from loopwatch.parsers.tables import (
    parse_cdp_neighbors,
    parse_interfaces_status,
    parse_lldp_neighbors,
    parse_mac_table,
)


def test_parse_mac_table(ios15_raw):
    macs = parse_mac_table(ios15_raw["show mac address-table dynamic"])
    assert MacEntry(10, "aabb.cc00.0002", "Gi1/0/14") in macs
    assert sum(1 for m in macs if m.port == "Gi1/0/48") == 40
    assert len(macs) == 42


def test_parse_mac_table_normalises_port_and_mac():
    text = "Vlan    Mac Address       Type        Ports\n----    -----------       --------    -----\n" \
           "  20    AABB.CC00.00FF    DYNAMIC     GigabitEthernet1/0/3\n"
    assert parse_mac_table(text) == (MacEntry(20, "aabb.cc00.00ff", "Gi1/0/3"),)


def test_parse_cdp_neighbors(ios15_raw):
    n = {x.port: x for x in parse_cdp_neighbors(ios15_raw["show cdp neighbors detail"])}
    assert set(n) == {"Gi1/0/48", "Gi1/0/14"}
    assert n["Gi1/0/48"].name.startswith("sw-core-1") and n["Gi1/0/48"].is_bridge
    assert n["Gi1/0/48"].remote_port == "Gi1/0/1" and n["Gi1/0/48"].platform == "cisco C9300-48P"
    assert n["Gi1/0/48"].capabilities == frozenset({"Switch", "IGMP"})
    assert n["Gi1/0/14"].name == "unmanaged-sw" and n["Gi1/0/14"].is_bridge


def test_parse_lldp_neighbors(ios15_raw):
    n = {x.port: x for x in parse_lldp_neighbors(ios15_raw["show lldp neighbors detail"])}
    assert n["Gi1/0/1"].name == "SEP001122334455"
    assert n["Gi1/0/1"].capabilities == frozenset({"B", "T"}) and not n["Gi1/0/1"].is_bridge


def test_parse_interfaces_status(ios15_raw):
    st = parse_interfaces_status(ios15_raw["show interfaces status"])
    assert st["Gi1/0/14"] == ("connected", "10", "a-1000")
    assert st["Gi1/0/48"][1] == "trunk"
    assert st["Gi1/0/2"][0] == "notconnect"


def test_parse_interfaces_status_err_disabled():
    text = "Port      Name               Status       Vlan       Duplex  Speed Type \n" \
           "Gi1/0/7                      err-disabled 10           auto   auto 10/100/1000BaseTX\n"
    assert parse_interfaces_status(text)["Gi1/0/7"][0] == "err-disabled"
