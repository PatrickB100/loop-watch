from datetime import timedelta

import pytest

from loopwatch.model import Event, Host, MacEntry, Neighbor
from loopwatch.report.diagnosis import vendor_of
from loopwatch.report.render import build_report, render_html, render_text
from loopwatch.store import Store
from tests.conftest import NOW
from tests.detectors.builders import make_event, make_snapshot

WINDOW = (NOW - timedelta(hours=1), NOW)
STUB_VENDOR = lambda mac: "V-" + mac[:4]  # noqa: E731


@pytest.fixture
def store():
    with Store(":memory:") as s:
        s.init_schema()
        yield s


@pytest.fixture
def store_bridge_scenario(store):
    snap = make_snapshot(
        neighbors=(Neighbor("Gi1/0/14", "TL-SG108", "1", "tp-link", frozenset({"B"})),),
        macs=(MacEntry(10, "a4b1.c2d3.e4f5", "Gi1/0/14"), MacEntry(10, "dc2c.6e01.aa10", "Te1/1/2")),
    )
    store.record_poll("sw-a", "10.0.0.5", NOW - timedelta(minutes=30), NOW - timedelta(minutes=30), snap, None)
    store.write_events([
        make_event("unexpected_neighbor", "Gi1/0/14", 20, severity="high"),
        make_event("mac_flap", "Gi1/0/14", 18, severity="high", mac="dc2c.6e01.aa10", other_port="Te1/1/2"),
        make_event("mac_flap", "Gi1/0/14", 18, severity="high", mac="0050.56a1.0b3c", other_port="Te1/1/2"),
    ])
    store.upsert_hosts([Host("0050.56a1.0b3c", "10.0.0.20", "fileserver01", True),
                        Host("a4b1.c2d3.e4f5", "10.0.10.183", "tp-link-switch", True)], "dhcp:ccr", NOW)
    return store


@pytest.fixture
def store_wireless_scenario(store):
    snap = make_snapshot(macs=(MacEntry(10, "3c22.fb10.9a01", "Gi1/0/14"), MacEntry(10, "aaaa.0000.0001", "Gi1/0/20")))
    store.record_poll("sw-a", "10.0.0.5", NOW - timedelta(minutes=30), NOW - timedelta(minutes=30), snap, None)
    store.write_events([
        make_event("mac_flap", "Gi1/0/14", 10, severity="high", mac="3c22.fb10.9a02", other_port="Gi1/0/20"),
        make_event("mac_flap", "Gi1/0/20", 10, severity="high", mac="3c22.fb10.9a02", other_port="Gi1/0/14"),
    ])
    store.upsert_hosts([Host("3c22.fb10.9a01", "10.0.10.57", "mbp-jdv", True),
                        Host("3c22.fb10.9a02", "10.0.10.58", "mbp-jdv", True)], "dhcp:ccr", NOW)
    return store


@pytest.fixture
def store_cable_scenario(store):
    store.write_events([
        make_event("mac_flap", "Gi1/0/3", 5, severity="high", mac="aaaa.bbbb.0001", other_port="Gi1/0/9"),
        make_event("mac_flap", "Gi1/0/9", 5, severity="high", mac="aaaa.bbbb.0001", other_port="Gi1/0/3"),
    ])
    return store


def test_bridge_on_edge_with_lldp_neighbor(store_bridge_scenario):
    d = build_report(store_bridge_scenario, {"sw-a", "sw-core-1"}, *WINDOW, vendor=STUB_VENDOR).diagnosis
    assert d is not None and d.pattern == "bridge_on_edge"
    assert d.connected is not None and d.connected.name == "TL-SG108"
    assert d.headline.endswith("a bridge appeared on this edge port")
    assert {h.other_port for h in d.flapping} == {"Te1/1/2"} and d.flapping[0].hostname == "fileserver01"
    assert [h.mac for h in d.attached] == ["a4b1.c2d3.e4f5"] and d.attached[0].hostname == "tp-link-switch"


def test_wireless_bridge_pattern_and_same_host_note(store_wireless_scenario):
    d = build_report(store_wireless_scenario, {"sw-a"}, *WINDOW, ap_ports={("sw-a", "Gi1/0/20")}, vendor=STUB_VENDOR).diagnosis
    assert d is not None and d.pattern == "wireless_bridge" and d.port == "Gi1/0/14"
    assert any("mbp-jdv is on both sides" in n for n in d.notes)
    assert d.attached[0].vendor.startswith("V-")


def test_cable_between_ports(store_cable_scenario):
    d = build_report(store_cable_scenario, {"sw-a"}, *WINDOW, vendor=STUB_VENDOR).diagnosis
    assert d is not None and d.pattern == "cable_between_ports" and "between Gi1/0/3 and Gi1/0/9" in d.headline


def test_no_suspects_gives_none_and_router_top_suspect_has_empty_lists(store):
    assert build_report(store, set(), *WINDOW, vendor=STUB_VENDOR).diagnosis is None
    store.write_events([Event(NOW - timedelta(minutes=1), "ccr", "broadcast_storm", "high", "storm", port="ether5")])
    d = build_report(store, {"ccr"}, *WINDOW, vendor=STUB_VENDOR).diagnosis
    assert d is not None and d.pattern == "unknown" and d.attached == () and d.flapping == ()
    assert "device is unknown" in " ".join(d.notes)


def test_vendor_of_known_and_unknown():
    assert "VMware" in vendor_of("0050.56a1.0b3c") and vendor_of("0200.0000.0001") == "unknown vendor"


def test_diagnosis_renders_first_in_text_and_html(store_bridge_scenario):
    data = build_report(store_bridge_scenario, {"sw-a", "sw-core-1"}, *WINDOW, vendor=STUB_VENDOR)
    text = render_text(data)
    assert text.index("Diagnosis") < text.index("Suspects") and "TL-SG108" in text
    html = render_html(data)
    assert "<h2>Diagnosis</h2>" in html and "TL-SG108" in html and html.index("Diagnosis") < html.index("Suspects")
