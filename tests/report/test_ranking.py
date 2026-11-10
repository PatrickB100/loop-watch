from loopwatch.model import Neighbor, StpVlan
from loopwatch.report.ranking import ap_ports_from_neighbors, rank_suspects, tc_chain, uplinks_from_neighbors
from tests.detectors.builders import make_event


def test_rank_suspects_weights_and_cap():
    evs = [make_event("mac_flap", "Gi1/0/14", 1, mac=f"aabb.cc00.{i:04x}") for i in range(8)]  # 8 MACs -> capped 100
    evs += [make_event("mac_flap", "Gi1/0/14", 1, mac="aabb.cc00.0000")]  # duplicate MAC does not add
    evs += [make_event("unexpected_neighbor", "Gi1/0/14", 1)]
    evs += [make_event("stp_topology_change", "Gi1/0/48", 1)] * 7  # 70 -> capped 50
    evs += [make_event("unreachable", None, 1)]  # no port: ignored
    top = rank_suspects(evs, uplinks={("sw-a", "Gi1/0/48")})
    assert [(s.switch, s.port, s.score) for s in top] == [("sw-a", "Gi1/0/14", 150), ("sw-a", "Gi1/0/48", 25)]
    assert len(top[0].events) == 10


def test_rank_suspects_other_weights():
    evs = [make_event("err_disable", "Gi1/0/7", 1), make_event("broadcast_storm", "Gi1/0/7", 1),
           make_event("link_flap", "Gi1/0/7", 1)]
    (s,) = rank_suspects(evs, uplinks=set())
    assert s.score == 80


def test_rank_suspects_top_ten():
    evs = [make_event("broadcast_storm", f"Gi1/0/{i}", 1) for i in range(15)]
    assert len(rank_suspects(evs, uplinks=set())) == 10


def test_rank_suspects_separates_switches():
    evs = [make_event("broadcast_storm", "Gi1/0/1", 1, switch="sw-a"), make_event("broadcast_storm", "Gi1/0/1", 1, switch="sw-b"),
           make_event("mac_flap", "Gi1/0/1", 1, switch="sw-b", mac="aabb.cc00.0001")]
    top = rank_suspects(evs, uplinks=set())
    assert [(s.switch, s.score) for s in top] == [("sw-b", 50), ("sw-a", 30)]


def test_uplinks_from_neighbors():
    n = {"sw-a": [Neighbor("Gi1/0/48", "sw-core-1.office.local", "Gi1/0/1", "C9300", frozenset({"S"})),
                  Neighbor("Gi1/0/14", "unmanaged-sw", "1", "SG250", frozenset({"S"}))]}
    assert uplinks_from_neighbors(n, {"sw-a", "sw-core-1"}) == {("sw-a", "Gi1/0/48")}


def test_tc_chain_follows_last_change_port_to_edge():
    stp = {"sw-core-1": [StpVlan(10, "r", 5, "Gi1/0/1", 300)], "sw-a": [StpVlan(10, "r", 12, "Gi1/0/14", 312)]}
    nb = {"sw-core-1": [Neighbor("Gi1/0/1", "sw-a", "Gi1/0/48", "", frozenset({"S"}))],
          "sw-a": [Neighbor("Gi1/0/14", "unmanaged-sw", "1", "", frozenset({"S"}))]}
    hops = tc_chain(10, stp, nb, start="sw-core-1")
    assert [(h.switch, h.port, h.next_switch) for h in hops] == [("sw-core-1", "Gi1/0/1", "sw-a"), ("sw-a", "Gi1/0/14", None)]


def test_tc_chain_stops_on_cycle():
    stp = {"a": [StpVlan(10, "r", 1, "Gi1", 1)], "b": [StpVlan(10, "r", 1, "Gi1", 1)]}
    nb = {"a": [Neighbor("Gi1", "b", "Gi1", "", frozenset({"S"}))], "b": [Neighbor("Gi1", "a", "Gi1", "", frozenset({"S"}))]}
    hops = tc_chain(10, stp, nb, start="a")
    assert [(h.switch, h.next_switch) for h in hops] == [("a", "b"), ("b", "a")]


def test_tc_chain_without_last_port_is_single_hop():
    stp = {"a": [StpVlan(10, "r", 1, None, None)]}
    assert tc_chain(10, stp, {}, start="a") == []


def test_ap_port_scores_half_and_is_labelled():
    events = [make_event("mac_flap", "Gi1/0/20", 1, mac="aaaa.bbbb.cccc"),
              make_event("mac_flap", "Gi1/0/14", 1, mac="aaaa.bbbb.cccc")]
    s = {x.port: x for x in rank_suspects(events, set(), {("sw-a", "Gi1/0/20")})}
    assert s["Gi1/0/20"].score == 10 and s["Gi1/0/20"].label == "ap"
    assert s["Gi1/0/14"].score == 20 and s["Gi1/0/14"].label is None


def test_uplink_label():
    events = [make_event("mac_flap", "Gi1/0/48", 1, mac="aaaa.bbbb.cccc")]
    (s,) = rank_suspects(events, {("sw-a", "Gi1/0/48")})
    assert s.label == "uplink" and s.score == 10


def test_ap_ports_from_lldp_capability():
    n = {"sw-a": [Neighbor("Gi1/0/20", "office-ap-1", "eth0", "Ubiquiti", frozenset({"B", "W"})),
                  Neighbor("Gi1/0/48", "sw-core-1", "Te1/1/2", "cisco", frozenset({"S"}))]}
    assert ap_ports_from_neighbors(n) == {("sw-a", "Gi1/0/20")}


def test_unicast_flood_scores_like_a_storm():
    (s,) = rank_suspects([make_event("unicast_flood", "Gi1/0/7", 1, severity="high")], uplinks=set())
    assert s.score == 30
