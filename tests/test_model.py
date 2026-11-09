from loopwatch.model import EVENT_KINDS, Neighbor


def test_neighbor_is_bridge():
    assert Neighbor("Gi1/0/14", "sw-x", "Gi0/1", "WS-C2960", frozenset({"S", "I"})).is_bridge
    assert not Neighbor("Gi1/0/2", "phone", "Port 1", "SEP", frozenset({"H", "P"})).is_bridge


def test_phone_advertising_bridge_and_telephone_is_not_a_bridge():
    # Cisco IP phones advertise LLDP capabilities B,T; CDP says "Host Phone Two-port Mac Relay".
    assert not Neighbor("Gi1/0/1", "SEP0011", "0011", "IP Phone", frozenset({"B", "T"})).is_bridge
    assert not Neighbor("Gi1/0/1", "SEP0011", "Port 1", "IP Phone", frozenset({"Host", "Phone", "Two-port", "Mac", "Relay"})).is_bridge


def test_event_kinds():
    assert EVENT_KINDS == {
        "stp_topology_change",
        "stp_role_change",
        "mac_flap",
        "broadcast_storm",
        "unicast_flood",
        "unexpected_neighbor",
        "link_flap",
        "err_disable",
        "unreachable",
        "parse_error",
    }


def test_unicast_flood_is_an_event_kind():
    from loopwatch.model import EVENT_KINDS

    assert "unicast_flood" in EVENT_KINDS
