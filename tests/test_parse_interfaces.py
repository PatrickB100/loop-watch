from loopwatch.parsers.interfaces import parse_interfaces
from loopwatch.parsers.tables import parse_interfaces_status


def test_parse_interfaces_counters(ios15_raw):
    status = parse_interfaces_status(ios15_raw["show interfaces status"])
    ifs = {i.port: i for i in parse_interfaces(ios15_raw["show interfaces"], status)}
    i14 = ifs["Gi1/0/14"]
    assert (i14.in_bcast, i14.in_mcast) == (4500, 1200)
    assert (i14.in_pkts, i14.out_pkts, i14.in_errors, i14.out_errors) == (2345678, 8765432, 2, 0)
    assert i14.link == "connected" and i14.vlan == "10" and i14.speed == "a-1000"
    assert "Vl1" not in ifs and set(ifs) == {"Gi1/0/1", "Gi1/0/14", "Gi1/0/48"}


def test_parse_interfaces_without_status(ios15_raw):
    ifs = {i.port: i for i in parse_interfaces(ios15_raw["show interfaces"], {})}
    assert ifs["Gi1/0/14"].link == "connected" and ifs["Gi1/0/14"].vlan == "" and ifs["Gi1/0/14"].speed == ""


def test_parse_interfaces_down_port_without_status():
    text = ("GigabitEthernet1/0/2 is down, line protocol is down (notconnect) \n"
            "  Hardware is Gigabit Ethernet, address is 0011.2233.4402 (bia 0011.2233.4402)\n"
            "     0 packets input, 0 bytes, 0 no buffer\n"
            "     Received 0 broadcasts (0 multicasts)\n"
            "     0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored\n"
            "     0 packets output, 0 bytes, 0 underruns\n"
            "     0 output errors, 0 collisions, 1 interface resets\n")
    (iface,) = parse_interfaces(text, {})
    assert iface.port == "Gi1/0/2" and iface.link == "notconnect" and iface.in_pkts == 0


def test_parse_interfaces_status_wins_over_line_protocol(ios15_raw):
    status = {"Gi1/0/14": ("err-disabled", "10", "auto")}
    ifs = {i.port: i for i in parse_interfaces(ios15_raw["show interfaces"], status)}
    assert ifs["Gi1/0/14"].link == "err-disabled"


def test_parse_interfaces_mgig_is_physical_and_appgig_is_not():
    text = ("TwoGigabitEthernet1/0/1 is up, line protocol is up (connected) \n"
            "     10 packets input, 100 bytes, 0 no buffer\n     Received 5 broadcasts (1 multicasts)\n"
            "     0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored\n"
            "     20 packets output, 200 bytes, 0 underruns\n     0 output errors, 0 collisions, 0 interface resets\n"
            "AppGigabitEthernet1/0/1 is up, line protocol is up (connected) \n"
            "     0 packets input, 0 bytes, 0 no buffer\n     Received 0 broadcasts (0 multicasts)\n")
    ifs = {i.port: i for i in parse_interfaces(text, {})}
    assert set(ifs) == {"Tw1/0/1"} and ifs["Tw1/0/1"].in_bcast == 5
