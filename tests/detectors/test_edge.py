from loopwatch.detectors import edge
from loopwatch.detectors.context import DetectorContext
from loopwatch.model import Neighbor
from tests.detectors.builders import make_snapshot, make_stp_port


def ctx(prev, cur):
    return DetectorContext(prev, cur, {}, [], frozenset())


BRIDGE = Neighbor("Gi1/0/14", "unmanaged-sw", "1", "SG250", frozenset({"S"}))
PHONE = Neighbor("Gi1/0/14", "SEP0011", "0011", "IP Phone", frozenset({"B", "T"}))


def test_bridge_on_edge_port_is_unexpected():
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge"),), neighbors=(BRIDGE,))
    (ev,) = edge.detect(ctx(None, cur))
    assert (ev.kind, ev.port, ev.severity) == ("unexpected_neighbor", "Gi1/0/14", "high")
    assert "unmanaged-sw" in ev.reason


def test_bridge_on_network_port_is_fine():
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="network"),), neighbors=(BRIDGE,))
    assert edge.detect(ctx(None, cur)) == []


def test_phone_on_edge_port_is_fine():
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge"),), neighbors=(PHONE,))
    assert edge.detect(ctx(None, cur)) == []


def test_same_bridge_already_present_is_not_repeated():
    snap = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge"),), neighbors=(BRIDGE,))
    assert edge.detect(ctx(snap, snap)) == []


def test_bridge_on_edge_port_in_one_of_many_vlans_fires_once():
    cur = make_snapshot(
        stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge"), make_stp_port(20, "Gi1/0/14", port_type="edge")),
        neighbors=(BRIDGE,),
    )
    assert len(edge.detect(ctx(None, cur))) == 1


def test_bpdu_increase_on_edge_port():
    prev = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge", bpdu_received=0),))
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge", bpdu_received=3),))
    (ev,) = edge.detect(ctx(prev, cur))
    assert (ev.kind, ev.port, ev.vlan, ev.severity) == ("unexpected_neighbor", "Gi1/0/14", 10, "high")
    assert "BPDU" in ev.reason and "3" in ev.reason


def test_bpdu_increase_on_network_port_is_normal():
    prev = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/48", port_type="network", bpdu_received=10),))
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/48", port_type="network", bpdu_received=40),))
    assert edge.detect(ctx(prev, cur)) == []


def test_bpdu_increase_on_port_that_was_edge_before():
    # IOS may drop operational portfast the moment a BPDU arrives; the previous snapshot proves it was an edge port.
    prev = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge", bpdu_received=0),))
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="network", bpdu_received=3),))
    (ev,) = edge.detect(ctx(prev, cur))
    assert ev.kind == "unexpected_neighbor" and ev.port == "Gi1/0/14"
