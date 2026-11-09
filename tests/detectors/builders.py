from datetime import timedelta

from loopwatch.model import Event, Interface, LogLine, Snapshot, StpPort, StpVlan
from tests.conftest import NOW


def make_iface(port: str, **kw) -> Interface:
    base = dict(port=port, link="connected", vlan="10", speed="a-1000", in_pkts=0, out_pkts=0,
                in_bcast=0, in_mcast=0, in_errors=0, out_errors=0)
    base.update(kw)
    return Interface(**base)


def make_stp_vlan(vlan: int, **kw) -> StpVlan:
    base = dict(vlan=vlan, root_id="4097 0011.2233.0001", tc_count=0, tc_last_port=None, tc_last_age_s=None)
    base.update(kw)
    return StpVlan(**base)


def make_stp_port(vlan: int, port: str, **kw) -> StpPort:
    base = dict(vlan=vlan, port=port, role="designated", state="forwarding", port_type="edge", bpdu_sent=0, bpdu_received=0)
    base.update(kw)
    return StpPort(**base)


def make_log(mnemonic: str, text: str, time=NOW) -> LogLine:
    return LogLine(time, mnemonic, f"%{mnemonic}: {text}")


def make_snapshot(**kw) -> Snapshot:
    base = dict(switch="sw-a", taken_at=NOW, stp_vlans=(), stp_ports=(), stp_summary=None, interfaces=(),
                macs=(), neighbors=(), log_lines=(), parse_errors=())
    base.update(kw)
    return Snapshot(**base)


def make_event(kind: str, port: str | None, minutes_ago: float, switch: str = "sw-a", severity: str = "medium", **kw) -> Event:
    return Event(NOW - timedelta(minutes=minutes_ago), switch, kind, severity, kw.pop("reason", kind), port=port, **kw)
