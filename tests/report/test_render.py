from dataclasses import replace
from datetime import timedelta

import pytest

from loopwatch.model import Event, Neighbor, StpVlan
from loopwatch.report.ranking import ChainHop, Suspect
from loopwatch.report.render import ReportData, build_report, render_html, render_text
from loopwatch.store import Store
from tests.conftest import NOW
from tests.detectors.builders import make_event, make_snapshot


@pytest.fixture
def seeded_store(snap_a):
    with Store(":memory:") as s:
        s.init_schema()
        core = make_snapshot(
            switch="sw-core-1",
            stp_vlans=(StpVlan(10, "4106 0011.2233.0001", 5, "Gi1/0/1", 300),),
            neighbors=(Neighbor("Gi1/0/1", "sw-a", "Gi1/0/48", "WS-C2960X", frozenset({"Switch"})),),
        )
        s.record_poll("sw-core-1", "10.0.0.1", NOW - timedelta(minutes=30), NOW - timedelta(minutes=30), core, None)
        s.record_poll("sw-a", "10.0.0.5", NOW - timedelta(minutes=30), NOW - timedelta(minutes=30), snap_a, None)
        s.write_events([
            make_event("unexpected_neighbor", "Gi1/0/14", 20, reason="bridge unmanaged-sw appeared on edge port Gi1/0/14"),
            make_event("stp_topology_change", "Gi1/0/14", 19, vlan=10),
            make_event("stp_topology_change", "Gi1/0/1", 19, switch="sw-core-1", vlan=10),
            make_event("mac_flap", "Gi1/0/48", 18, mac="aabb.cc00.0002", vlan=10, severity="high"),
            make_event("broadcast_storm", "Gi1/0/14", 17, severity="high"),
        ])
        yield s


def test_build_report_ranks_and_chains(seeded_store):
    data = build_report(seeded_store, {"sw-a", "sw-core-1"}, NOW - timedelta(hours=1), NOW)
    assert data.window == (NOW - timedelta(hours=1), NOW)
    assert (data.suspects[0].switch, data.suspects[0].port) == ("sw-a", "Gi1/0/14")
    assert len(data.events) == 5
    assert [(h.switch, h.port, h.next_switch) for h in data.chains[10]] == [
        ("sw-core-1", "Gi1/0/1", "sw-a"), ("sw-a", "Gi1/0/14", None)]
    # Gi1/0/48 on sw-a faces sw-core-1: an uplink, so its mac_flap score is halved
    gi48 = next(s for s in data.suspects if s.port == "Gi1/0/48")
    assert gi48.score == 10


def test_build_report_empty_window(seeded_store):
    data = build_report(seeded_store, {"sw-a"}, NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    assert data.suspects == [] and data.events == [] and data.chains == {}


@pytest.fixture
def report_data(seeded_store):
    return build_report(seeded_store, {"sw-a", "sw-core-1"}, NOW - timedelta(hours=1), NOW)


def test_render_text_contains_sections(report_data):
    text = render_text(report_data)
    assert "Suspects" in text and "Topology change chain" in text and "Timeline" in text
    assert "sw-a Gi1/0/14" in text
    assert "sw-core-1 Gi1/0/1 -> sw-a Gi1/0/14 -> (unmanaged)" in text
    assert "09:40:00 sw-a" in text and "unexpected_neighbor" in text


def test_render_text_without_events():
    data = ReportData((NOW, NOW), [], [], {})
    text = render_text(data)
    assert "no events" in text.lower()


def test_render_html_escapes_and_lists_events(report_data):
    nasty = replace(report_data.events[0], reason="<script>alert(1)</script>")
    data = ReportData(report_data.window, report_data.suspects, [nasty] + report_data.events[1:], report_data.chains)
    html = render_html(data)
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "<table" in html and "Gi1/0/14" in html and "<!doctype html>" in html.lower()
