from loopwatch.model import EVENT_KINDS, Neighbor


def test_neighbor_is_bridge():
    assert Neighbor("Gi1/0/14", "sw-x", "Gi0/1", "WS-C2960", frozenset({"S", "I"})).is_bridge
    assert not Neighbor("Gi1/0/2", "phone", "Port 1", "SEP", frozenset({"H", "P"})).is_bridge


def test_event_kinds():
    assert EVENT_KINDS == {
        "stp_topology_change",
        "stp_role_change",
        "mac_flap",
        "broadcast_storm",
        "unexpected_neighbor",
        "link_flap",
        "err_disable",
        "unreachable",
        "parse_error",
    }
