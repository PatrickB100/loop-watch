from dataclasses import replace
from datetime import datetime, timedelta, timezone

from loopwatch.detectors.context import DetectorContext
from loopwatch.detectors.runner import run_detectors
from loopwatch.model import Host
from loopwatch.parsers.mikrotik import (
    MIKROTIK_COMMANDS,
    parse_dhcp_leases,
    parse_ethernet_stats,
    parse_interface_list,
    parse_ip_neighbors,
    parse_log,
    parse_mikrotik_snapshot,
)
from loopwatch.profiles import MIKROTIK_ROUTEROS, profile_for, version_of
from loopwatch.transport import command_slug
from tests.conftest import FIXTURES, NOW

ROOT = FIXTURES / "routeros7" / "ccr"


def raw() -> dict[str, str]:
    return {c: (ROOT / f"{command_slug(c)}.txt").read_text() for c in MIKROTIK_COMMANDS}


def test_interface_list_link_states():
    links = parse_interface_list(raw()[MIKROTIK_COMMANDS[0]])
    assert links == {"ether1": "connected", "ether2": "notconnect", "ether3": "disabled",
                     "ether5": "connected", "sfp-sfpplus1": "connected"}


def test_ethernet_stats_join_across_blocks():
    stats = parse_ethernet_stats(raw()[MIKROTIK_COMMANDS[1]])
    assert stats["ether5"]["rx-broadcast"] == 12000 and stats["sfp-sfpplus1"]["rx-packet"] == 400000
    assert stats["ether3"]["rx-packet"] == 0 and len(stats) == 5


def test_ip_neighbors_capabilities_mapped():
    n = {x.port: x for x in parse_ip_neighbors(raw()[MIKROTIK_COMMANDS[2]])}
    assert n["ether5"].name == "sw-floor2-a" and n["ether5"].remote_port == "Gi1/0/48" and n["ether5"].is_bridge
    assert n["ether2"].capabilities == frozenset({"B", "W"}) and "Ubiquiti" in n["ether2"].platform


def test_log_keeps_relevant_topics_and_maps_link_lines():
    lines = parse_log(raw()[MIKROTIK_COMMANDS[3]], NOW)
    texts = [l.text for l in lines]
    assert "ether5 link down" in texts and "bridge1: topology change" in texts
    assert any("login failure" in t for t in texts) and "tp-link-switch" in " ".join(texts)
    assert not any("firewall" in (l.mnemonic or "") for l in lines) and not any("logged in" in t for t in texts)
    down = next(l for l in lines if l.text == "ether5 link down")
    assert down.mnemonic == "LINK-3-UPDOWN" and down.time == NOW.replace(hour=10, minute=42, second=11)
    assert next(l for l in lines if "tp-link-switch" in l.text).time == datetime(2026, 11, 8, 23, 59, 58, tzinfo=timezone.utc)
    assert next(l for l in lines if "topology" in l.text).mnemonic == "bridge,stp"


def test_dhcp_leases():
    hosts = {h.mac: h for h in parse_dhcp_leases(raw()[MIKROTIK_COMMANDS[4]])}
    assert hosts["a4b1.c2d3.e4f5"] == Host("a4b1.c2d3.e4f5", "10.0.10.183", "tp-link-switch", True)
    assert hosts["3c22.fb10.9a01"].active is False


def test_snapshot_shape_and_profile():
    snap = parse_mikrotik_snapshot("ccr", raw(), NOW)
    by = {i.port: i for i in snap.interfaces}
    assert by["ether5"].link == "connected" and by["ether5"].in_bcast == 12000 and by["ether5"].in_errors == 3
    assert snap.stp_vlans == () and snap.macs == () and snap.stp_summary is None and len(snap.hosts) == 2
    assert snap.parse_errors == () and len(snap.neighbors) == 2
    assert profile_for("mikrotik_routeros") is MIKROTIK_ROUTEROS
    assert version_of(MIKROTIK_ROUTEROS, (ROOT / "_system_resource_print.txt").read_text()) == "7.16.1"


def test_empty_lease_table_is_not_a_parse_error():
    r = raw()
    r[MIKROTIK_COMMANDS[4]] = ""
    assert parse_mikrotik_snapshot("ccr", r, NOW).parse_errors == ()


def test_router_storm_fires_but_stp_and_mac_stay_silent():
    t0 = parse_mikrotik_snapshot("ccr", raw(), NOW)
    r = raw()
    r[MIKROTIK_COMMANDS[1]] = r[MIKROTIK_COMMANDS[1]].replace("rx-broadcast:  12000", "rx-broadcast:  2012000")
    t1 = replace(parse_mikrotik_snapshot("ccr", r, NOW + timedelta(seconds=60)), taken_at=NOW + timedelta(seconds=60))
    events = run_detectors(DetectorContext(t0, t1, {"ether5": 100.0}, [], frozenset()))
    assert {e.kind for e in events} == {"broadcast_storm"} and events[0].port == "ether5"
