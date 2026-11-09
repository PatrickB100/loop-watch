"""Every recorded fixture set must parse without errors and yield the expected shape."""

import pytest

from loopwatch.inventory import COMMANDS
from loopwatch.parsers.snapshot import parse_snapshot
from loopwatch.transport import command_slug
from tests.conftest import FIXTURES, NOW

EXPECTED = {
    "ios15/sw-a": dict(vlans=[1, 10], edge_ports={"Gi1/0/1", "Gi1/0/14"}, uplink="Gi1/0/48", neighbors=3,
                       macs=42, interfaces=3, log_lines=4, mode="rapid-pvst", bpduguard=True),
    "ios15/sw-core-1": dict(vlans=[10], edge_ports=set(), uplink="Gi1/0/1", neighbors=1,
                            macs=2, interfaces=1, log_lines=1, mode="rapid-pvst", bpduguard=True),
    "iosxe17/sw-9300": dict(vlans=[10], edge_ports={"Gi1/0/5"}, uplink="Twe1/1/1", neighbors=2,
                            macs=3, interfaces=2, log_lines=3, mode="rapid-pvst", bpduguard=True),
}


def load(set_name: str) -> dict[str, str]:
    folder = FIXTURES / set_name
    return {c: (folder / f"{command_slug(c)}.txt").read_text() for c in COMMANDS}


@pytest.mark.parametrize("set_name", sorted(EXPECTED))
def test_fixture_set_parses_cleanly(set_name):
    exp = EXPECTED[set_name]
    s = parse_snapshot(set_name.split("/")[1], load(set_name), NOW)
    assert s.parse_errors == ()
    assert [v.vlan for v in s.stp_vlans] == exp["vlans"]
    assert {p.port for p in s.stp_ports if p.port_type == "edge"} == exp["edge_ports"]
    uplink = next(p for p in s.stp_ports if p.port == exp["uplink"])
    assert uplink.port_type == "network"
    assert all(v.tc_last_port for v in s.stp_vlans)
    assert len(s.neighbors) == exp["neighbors"] and len(s.macs) == exp["macs"]
    assert len(s.interfaces) == exp["interfaces"] and len(s.log_lines) == exp["log_lines"]
    assert s.stp_summary is not None and s.stp_summary.mode == exp["mode"]
    assert s.stp_summary.bpduguard_default is exp["bpduguard"]
    assert all(i.link in ("connected", "notconnect", "disabled", "err-disabled") for i in s.interfaces)
    assert all(line.time is not None for line in s.log_lines)


def test_iosxe_uplink_counters_and_status():
    s = parse_snapshot("sw-9300", load("iosxe17/sw-9300"), NOW)
    up = next(i for i in s.interfaces if i.port == "Twe1/1/1")
    assert (up.in_bcast, up.in_mcast, up.link, up.vlan, up.speed) == (2345678, 1234567, "connected", "trunk", "10G")
    core = next(n for n in s.neighbors if n.port == "Twe1/1/1")
    assert core.name.startswith("sw-core-1") and core.remote_port == "Twe1/1/2" and core.is_bridge
    phone = next(n for n in s.neighbors if n.port == "Gi1/0/5")
    assert not phone.is_bridge
