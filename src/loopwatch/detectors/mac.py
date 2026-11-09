import re

from loopwatch.detectors.context import DetectorContext, log_reason, new_log_lines
from loopwatch.model import Event
from loopwatch.ports import normalize_mac, normalize_port

_MACFLAP = re.compile(r"Host (\S+) in vlan (\d+) is flapping between port (\S+) and port (\S+)")


def detect(ctx: DetectorContext) -> list[Event]:
    events: list[Event] = []
    now = ctx.cur.taken_at
    if ctx.prev is not None:
        before = {(m.vlan, m.mac): m.port for m in ctx.prev.macs}
        for entry in ctx.cur.macs:
            old_port = before.get((entry.vlan, entry.mac))
            if old_port is None or old_port == entry.port:
                continue
            events.append(
                Event(
                    time=now,
                    switch=ctx.cur.switch,
                    kind="mac_flap",
                    severity="high",
                    reason=f"{entry.mac} moved from {old_port} to {entry.port}",
                    port=entry.port,
                    vlan=entry.vlan,
                    mac=entry.mac,
                    other_port=old_port,
                )
            )
    for line in new_log_lines(ctx):
        if line.mnemonic != "SW_MATM-4-MACFLAP_NOTIF":
            continue
        m = _MACFLAP.search(line.text)
        events.append(
            Event(
                time=now,
                switch=ctx.cur.switch,
                kind="mac_flap",
                severity="high",
                reason=log_reason(line),
                port=normalize_port(m.group(3)) if m else None,
                vlan=int(m.group(2)) if m else None,
                mac=normalize_mac(m.group(1)) if m else None,
                other_port=normalize_port(m.group(4)) if m else None,
            )
        )
    return events
