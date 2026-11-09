import pytest

from loopwatch.ports import normalize_mac, normalize_port


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("GigabitEthernet1/0/14", "Gi1/0/14"),
        ("Gi1/0/14", "Gi1/0/14"),
        ("TenGigabitEthernet1/1/1", "Te1/1/1"),
        ("TwentyFiveGigE1/0/1", "Twe1/0/1"),
        ("FortyGigabitEthernet1/1/1", "Fo1/1/1"),
        ("HundredGigE1/0/25", "Hu1/0/25"),
        ("FastEthernet0/1", "Fa0/1"),
        ("Port-channel1", "Po1"),
        ("Po1", "Po1"),
        ("Vlan10", "Vl10"),
        (" GigabitEthernet1/0/14 ", "Gi1/0/14"),
        ("TwoGigabitEthernet1/0/1", "Tw1/0/1"),
        ("Tw1/0/1", "Tw1/0/1"),
        ("AppGigabitEthernet1/0/1", "Ap1/0/1"),
        ("gigabitethernet1/0/14", "Gi1/0/14"),
    ],
)
def test_normalize_port(raw, expected):
    assert normalize_port(raw) == expected


def test_normalize_port_unknown_prefix_is_returned_stripped():
    assert normalize_port(" Loopback0 ") == "Loopback0"


@pytest.mark.parametrize("raw", ["AABB.CCDD.EEFF", "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "aabbccddeeff"])
def test_normalize_mac(raw):
    assert normalize_mac(raw) == "aabb.ccdd.eeff"


def test_routeros_names_are_left_alone():
    assert normalize_port("ether5") == "ether5" and normalize_port("sfp-sfpplus1") == "sfp-sfpplus1"
