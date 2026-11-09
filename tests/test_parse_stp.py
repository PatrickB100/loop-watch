import pytest

from loopwatch.parsers.stp import parse_age, parse_stp_detail, parse_stp_summary


@pytest.fixture
def stp_detail_text(ios15_raw):
    return ios15_raw["show spanning-tree detail"]


@pytest.fixture
def stp_summary_text(ios15_raw):
    return ios15_raw["show spanning-tree summary"]


def test_parse_stp_detail_vlans(stp_detail_text):
    vlans, _ = parse_stp_detail(stp_detail_text)
    assert [v.vlan for v in vlans] == [1, 10]
    v10 = next(v for v in vlans if v.vlan == 10)
    assert v10.tc_count == 12 and v10.tc_last_port == "Gi1/0/14" and v10.tc_last_age_s == 312
    assert v10.root_id == "4106 0011.2233.0001"


def test_parse_stp_detail_ports(stp_detail_text):
    _, ports = parse_stp_detail(stp_detail_text)
    assert len(ports) == 4
    p14 = next(p for p in ports if p.port == "Gi1/0/14" and p.vlan == 10)
    assert (p14.role, p14.state, p14.port_type, p14.bpdu_sent, p14.bpdu_received) == (
        "designated", "forwarding", "edge", 5678, 3)
    p48 = next(p for p in ports if p.port == "Gi1/0/48" and p.vlan == 10)
    assert (p48.role, p48.state, p48.port_type) == ("root", "forwarding", "network")


@pytest.mark.parametrize(
    "raw,secs",
    [("00:05:12", 312), ("1d02h", 93600), ("3w2d", 1987200), ("2y5w", 2 * 31536000 + 5 * 604800), ("Never", None)],
)
def test_parse_age(raw, secs):
    assert parse_age(raw) == secs


def test_parse_stp_summary(stp_summary_text):
    s = parse_stp_summary(stp_summary_text)
    assert s.mode == "rapid-pvst"
    assert s.portfast_default is False and s.bpduguard_default is True and s.loopguard_default is False


def test_parse_stp_summary_iosxe_wording():
    text = "Switch is in rapid-pvst mode\nPortfast Default                        is enabled\n" \
           "Portfast Edge BPDU Guard Default        is enabled\nLoopguard Default                       is enabled\n"
    s = parse_stp_summary(text)
    assert s.portfast_default is True and s.bpduguard_default is True and s.loopguard_default is True
