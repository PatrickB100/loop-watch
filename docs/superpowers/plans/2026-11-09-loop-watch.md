# loop-watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Docker service that polls Cisco Catalyst switches over SSH, detects the signs of a layer-2 loop, and reports which port it entered on.

**Architecture:** One Python package `loopwatch` with pure parsers and detectors (plain data in, plain data out), a thin SSH transport with a fixture-replaying fake, a SQLite store, an asyncio collector that schedules polls and escalates on events, and a report module that ranks suspect ports. Three CLI commands share the store.

**Tech Stack:** Python 3.12, netmiko, ntc-templates, PyYAML, click, Jinja2, sqlite3 (stdlib), pytest. Docker with compose.

**Spec:** `docs/superpowers/specs/2026-11-09-loop-watch-design.md`

## Global Constraints

- Python 3.12; `src/` layout; package name `loopwatch`; console script `loop-watch`.
- Runtime dependencies are exactly: netmiko, ntc-templates, PyYAML, click, Jinja2. Dev: pytest. Nothing else without asking.
- `parsers` and `detectors` import nothing from `transport`, `store`, `collector` or `cli`.
- Credentials come only from environment variables `LOOPWATCH_SSH_USER`, `LOOPWATCH_SSH_PASSWORD`, `LOOPWATCH_SSH_ENABLE`. They never appear in the database, log output, reports, exceptions or `repr()`.
- Canonical port names are the short IOS form (`Gi1/0/14`, `Te1/1/1`, `Po1`). Every parser converts to it.
- MAC addresses are stored lowercase in IOS dotted form `aabb.ccdd.eeff`.
- Times are timezone-aware UTC `datetime`s in code and ISO-8601 strings in SQLite.
- Intervals and thresholds: normal poll 60 s, escalated 10 s for 600 s, SSH connect timeout 15 s, command timeout 30 s, at most 8 concurrent sessions, storm = 20× one-hour rolling median and ≥ 1000 pps, snapshot retention 14 days, events 180 days.
- No test opens a network connection. All switch output comes from `tests/fixtures/`.
- Commits follow Conventional Commits, one logical change each; every commit passes `pytest`.

## Review Focus

1. Counters that go backwards (switch reboot, `clear counters`) must give an unknown rate, never a storm event. Pinned in Task 11.
2. Port names in every IOS spelling (`GigabitEthernet`, `Gi`, `TenGigabitEthernet`, `TwentyFiveGigE`, `FortyGigabitEthernet`, `HundredGigE`, `FastEthernet`, `Port-channel`) must normalise to one canonical form, or MAC flaps between the MAC table and STP output are invented. Pinned in Task 1.
3. `show logging` timestamps come with sequence numbers, `*` or `.` prefixes, milliseconds and timezone names, and no year; a line that cannot be parsed must still be kept with `time=None`. Pinned in Task 7.
4. A command that returns `% Invalid input detected` or empty output must produce a `parse_error` and leave the rest of the snapshot intact. Pinned in Task 8.
5. A password must never leak through an exception message or `repr()` of inventory or transport objects. Pinned in Tasks 2 and 3.

---

### Task 1: Project scaffold and data model

**Files:**
- Create: `pyproject.toml`, `README.md`, `.env.example`, `src/loopwatch/__init__.py`, `src/loopwatch/model.py`, `src/loopwatch/ports.py`, `tests/__init__.py`, `tests/test_ports.py`, `tests/test_model.py`

**Interfaces:**
- Produces, in `model.py` (all `@dataclass(frozen=True)` unless noted):
  - `StpVlan(vlan: int, root_id: str, tc_count: int, tc_last_port: str | None, tc_last_age_s: int | None)`
  - `StpPort(vlan: int, port: str, role: str, state: str, port_type: str, bpdu_sent: int, bpdu_received: int)` where `role` is one of `root|designated|alternate|backup|disabled`, `state` one of `forwarding|blocking|learning|listening|disabled`, `port_type` one of `edge|network|normal` (`edge` when the detail output says `portfast` or `edge`).
  - `StpSummary(mode: str, portfast_default: bool, bpduguard_default: bool, loopguard_default: bool)`
  - `Interface(port: str, link: str, vlan: str, speed: str, in_pkts: int, out_pkts: int, in_bcast: int, in_mcast: int, in_errors: int, out_errors: int)` where `link` is one of `connected|notconnect|disabled|err-disabled|unknown`.
  - `MacEntry(vlan: int, mac: str, port: str)`
  - `Neighbor(port: str, name: str, remote_port: str, platform: str, capabilities: frozenset[str])` with property `is_bridge -> bool` true when capabilities intersect `{"S", "B", "Switch", "Bridge"}`.
  - `LogLine(time: datetime | None, mnemonic: str | None, text: str)` where `mnemonic` is the `%FACILITY-N-NAME` token without `%`.
  - `ParseError(command: str, message: str)`
  - `Snapshot(switch: str, taken_at: datetime, stp_vlans: tuple[StpVlan, ...], stp_ports: tuple[StpPort, ...], stp_summary: StpSummary | None, interfaces: tuple[Interface, ...], macs: tuple[MacEntry, ...], neighbors: tuple[Neighbor, ...], log_lines: tuple[LogLine, ...], parse_errors: tuple[ParseError, ...])`
  - `Event(time: datetime, switch: str, kind: str, severity: str, reason: str, port: str | None = None, vlan: int | None = None, mac: str | None = None)` with `severity` one of `low|medium|high`.
  - `EVENT_KINDS: frozenset[str]` = the eight kinds from the spec plus `parse_error`.
- Produces, in `ports.py`: `normalize_port(name: str) -> str` and `normalize_mac(mac: str) -> str`.

- [ ] **Step 1: Write `pyproject.toml`**

PEP 621 with hatchling, `requires-python = ">=3.12"`, the five runtime dependencies, `[project.optional-dependencies] dev = ["pytest"]`, `[project.scripts] loop-watch = "loopwatch.cli:main"`, `[tool.pytest.ini_options] testpaths = ["tests"]`. `.env.example` lists the three variables with empty values. `README.md` holds a one-paragraph purpose and a "see docs/superpowers/specs" line; the full README comes in Task 16.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_ports.py
import pytest
from loopwatch.ports import normalize_port, normalize_mac

@pytest.mark.parametrize("raw,expected", [
    ("GigabitEthernet1/0/14", "Gi1/0/14"), ("Gi1/0/14", "Gi1/0/14"),
    ("TenGigabitEthernet1/1/1", "Te1/1/1"), ("TwentyFiveGigE1/0/1", "Twe1/0/1"),
    ("FortyGigabitEthernet1/1/1", "Fo1/1/1"), ("HundredGigE1/0/25", "Hu1/0/25"),
    ("FastEthernet0/1", "Fa0/1"), ("Port-channel1", "Po1"), ("Po1", "Po1"),
    ("Vlan10", "Vl10"), (" GigabitEthernet1/0/14 ", "Gi1/0/14"),
])
def test_normalize_port(raw, expected):
    assert normalize_port(raw) == expected

def test_normalize_port_unknown_prefix_is_returned_stripped():
    assert normalize_port("Loopback0") == "Loopback0"

@pytest.mark.parametrize("raw", ["AABB.CCDD.EEFF", "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "aabbccddeeff"])
def test_normalize_mac(raw):
    assert normalize_mac(raw) == "aabb.ccdd.eeff"
```

```python
# tests/test_model.py
from loopwatch.model import Neighbor, EVENT_KINDS

def test_neighbor_is_bridge():
    assert Neighbor("Gi1/0/14", "sw-x", "Gi0/1", "WS-C2960", frozenset({"S", "I"})).is_bridge
    assert not Neighbor("Gi1/0/2", "phone", "Port 1", "SEP", frozenset({"H", "P"})).is_bridge

def test_event_kinds():
    assert EVENT_KINDS == {"stp_topology_change", "stp_role_change", "mac_flap", "broadcast_storm",
                           "unexpected_neighbor", "link_flap", "err_disable", "unreachable", "parse_error"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pip install -e '.[dev]' && pytest -q`
Expected: ImportError on `loopwatch.ports` / `loopwatch.model`.

- [ ] **Step 4: Implement `model.py` and `ports.py`**

`normalize_port`: strip, then replace a leading long name by its short form using an ordered mapping (`TwentyFiveGigE→Twe` before `Te`-like matches; `TenGigabitEthernet→Te`, `GigabitEthernet→Gi`, `FortyGigabitEthernet→Fo`, `HundredGigE→Hu`, `FastEthernet→Fa`, `Port-channel→Po`, `Vlan→Vl`). Unknown prefixes pass through stripped. `normalize_mac`: drop every non-hex character, lowercase, insert dots after 4 and 8 hex digits.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -q` — Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml README.md .env.example src tests
git commit -m "feat: scaffold loopwatch package with data model and port normalisation"
```

---

### Task 2: Inventory

**Files:**
- Create: `src/loopwatch/inventory.py`, `tests/test_inventory.py`, `tests/fixtures/inventory/basic.yaml`

**Interfaces:**
- Produces: `Credentials(user: str, password: str, enable: str | None)` dataclass with `__repr__` returning `Credentials(user='…', password='***')`.
- `Switch(name: str, host: str, device_type: str, poll_interval: int, skip: frozenset[str])`.
- `Inventory(switches: tuple[Switch, ...], credentials: Credentials)` with `by_name(name) -> Switch`.
- `load_inventory(path: Path, env: Mapping[str, str]) -> Inventory`. Raises `InventoryError` (subclass of `ValueError`) on duplicate names, missing `name`/`host`, or missing `LOOPWATCH_SSH_USER` / `LOOPWATCH_SSH_PASSWORD`.
- `COMMANDS: tuple[str, ...]` = the eight spec commands in spec order: `show spanning-tree detail`, `show spanning-tree summary`, `show mac address-table dynamic`, `show interfaces`, `show interfaces status`, `show cdp neighbors detail`, `show lldp neighbors detail`, `show logging`.
- `Switch.commands -> tuple[str, ...]`: `COMMANDS` minus `skip`.

- [ ] **Step 1: Write the fixture `tests/fixtures/inventory/basic.yaml`**

Exactly the YAML from the spec's Configuration section (two switches, `sw-floor2-a` skips `show cdp neighbors detail`, defaults block).

- [ ] **Step 2: Write the failing tests**

```python
def test_load_inventory_applies_defaults_and_skip(fixture_path):
    inv = load_inventory(fixture_path, {"LOOPWATCH_SSH_USER": "ro", "LOOPWATCH_SSH_PASSWORD": "pw"})
    assert [s.name for s in inv.switches] == ["sw-core-1", "sw-floor2-a"]
    assert inv.by_name("sw-core-1").poll_interval == 60
    assert inv.by_name("sw-core-1").device_type == "cisco_ios"
    assert "show cdp neighbors detail" not in inv.by_name("sw-floor2-a").commands
    assert len(inv.by_name("sw-core-1").commands) == 8

def test_missing_credentials_raises(fixture_path):
    with pytest.raises(InventoryError): load_inventory(fixture_path, {})

def test_duplicate_names_raise(tmp_path): ...  # write YAML with the same name twice

def test_credentials_repr_hides_password():
    assert "hunter2" not in repr(Credentials("ro", "hunter2", None))
    assert "hunter2" not in str(Inventory((), Credentials("ro", "hunter2", None)))
```

- [ ] **Step 3: Run tests to verify they fail** — `pytest tests/test_inventory.py -q`, ImportError.

- [ ] **Step 4: Implement `inventory.py`** with `yaml.safe_load`; `env` is a parameter so tests never touch `os.environ`.

- [ ] **Step 5: Run tests to verify they pass.**

- [ ] **Step 6: Commit** — `feat(inventory): load switch list and credentials from yaml and env`

---

### Task 3: Transport with fixture-replaying fake

**Files:**
- Create: `src/loopwatch/transport.py`, `tests/test_transport.py`, `tests/fixtures/ios15/sw-a/*.txt` (eight files, see Step 1)

**Interfaces:**
- Produces: `class Transport(Protocol): def run(self, switch: Switch, credentials: Credentials, commands: Sequence[str]) -> dict[str, str]`. Raises `TransportError(switch_name: str, message: str)` (subclass `RuntimeError`); message never includes credentials.
- `command_slug(command: str) -> str`: `show spanning-tree detail` → `show_spanning-tree_detail` (spaces to underscores, nothing else changed).
- `FakeTransport(root: Path)`: reads `root/<switch.name>/<command_slug>.txt`; a missing file raises `TransportError`; a file named `root/<switch.name>/UNREACHABLE` makes every call raise `TransportError`. Also records `calls: list[tuple[str, tuple[str, ...]]]`.
- `NetmikoTransport(connect_timeout: int = 15, command_timeout: int = 30)`: uses `netmiko.ConnectHandler(device_type=switch.device_type, host=switch.host, username=..., password=..., secret=..., conn_timeout=15, read_timeout_override=30)`, sends `terminal length 0` once, then `send_command(cmd, read_timeout=30)` per command. Wraps every netmiko exception in `TransportError` with the exception class name and `str(exc)` with the password replaced by `***`.

- [ ] **Step 1: Write fixture files for `tests/fixtures/ios15/sw-a/`**

Eight files named by `command_slug`, content in Cisco IOS 15.2(7)E Catalyst 2960-X format with hostname `sw-a`. Their content is pinned by the parser tests in Tasks 4–7; write them to satisfy those tests, in particular: VLAN0001 and VLAN0010 in spanning-tree detail, VLAN0010 with `Number of topology changes 12 last change occurred 00:05:12 ago from GigabitEthernet1/0/14`, ports Gi1/0/1 (edge, designated forwarding, BPDU sent 1234 received 0), Gi1/0/14 (edge, designated forwarding, BPDU received 3), Gi1/0/48 (network, root forwarding); MAC table with `aabb.cc00.0001` on Gi1/0/1 vlan 10 and `aabb.cc00.0002` on Gi1/0/14 vlan 10 and 40 entries on Gi1/0/48; interfaces Gi1/0/1, Gi1/0/14, Gi1/0/48 with `Received 4500 broadcasts (1200 multicasts)` on Gi1/0/14; CDP neighbour `sw-core-1` (capability `Switch IGMP`) on Gi1/0/48 and `unmanaged-sw` (`Switch`) on Gi1/0/14; LLDP neighbour `SEP001122334455` on Gi1/0/1; logging buffer with the five lines listed in Task 7 Step 1.

- [ ] **Step 2: Write the failing tests**

```python
def test_fake_transport_returns_all_commands(ios15_root, sw_a, creds):
    out = FakeTransport(ios15_root).run(sw_a, creds, COMMANDS)
    assert set(out) == set(COMMANDS)
    assert "VLAN0010" in out["show spanning-tree detail"]

def test_fake_transport_unreachable(tmp_path, sw_a, creds):
    (tmp_path / "sw-a").mkdir(); (tmp_path / "sw-a" / "UNREACHABLE").touch()
    with pytest.raises(TransportError): FakeTransport(tmp_path).run(sw_a, creds, COMMANDS)

def test_command_slug():
    assert command_slug("show spanning-tree detail") == "show_spanning-tree_detail"

def test_transport_error_redacts_password():
    err = NetmikoTransport._wrap("sw-a", Exception("auth failed for pw hunter2"), "hunter2")
    assert "hunter2" not in str(err)
```

- [ ] **Step 3: Run tests to verify they fail.**

- [ ] **Step 4: Implement `transport.py`**, with `NetmikoTransport._wrap(switch_name, exc, password) -> TransportError` as a static method so redaction is testable without a connection. Netmiko is imported inside `NetmikoTransport.run` so tests never import it.

- [ ] **Step 5: Run tests to verify they pass.**

- [ ] **Step 6: Commit** — `feat(transport): add netmiko transport and fixture-replaying fake`

---

### Task 4: Spanning-tree parsers

**Files:**
- Create: `src/loopwatch/parsers/__init__.py`, `src/loopwatch/parsers/stp.py`, `tests/test_parse_stp.py`

**Interfaces:**
- Produces: `parse_stp_detail(text: str) -> tuple[tuple[StpVlan, ...], tuple[StpPort, ...]]` and `parse_stp_summary(text: str) -> StpSummary`.
- Age strings `00:05:12`, `1d02h`, `3w2d`, `2y5w` convert to seconds via `parse_age(text: str) -> int | None` (exported, tested). `Never` → `None`.

- [ ] **Step 1: Write the failing tests** (using the Task 3 fixture)

```python
def test_parse_stp_detail_vlans(stp_detail_text):
    vlans, ports = parse_stp_detail(stp_detail_text)
    v10 = next(v for v in vlans if v.vlan == 10)
    assert v10.tc_count == 12 and v10.tc_last_port == "Gi1/0/14" and v10.tc_last_age_s == 312

def test_parse_stp_detail_ports(stp_detail_text):
    _, ports = parse_stp_detail(stp_detail_text)
    p14 = next(p for p in ports if p.port == "Gi1/0/14" and p.vlan == 10)
    assert (p14.role, p14.state, p14.port_type, p14.bpdu_received) == ("designated", "forwarding", "edge", 3)
    p48 = next(p for p in ports if p.port == "Gi1/0/48" and p.vlan == 10)
    assert (p48.role, p48.port_type) == ("root", "network")

@pytest.mark.parametrize("raw,secs", [("00:05:12", 312), ("1d02h", 93600), ("3w2d", 1987200), ("Never", None)])
def test_parse_age(raw, secs): assert parse_age(raw) == secs

def test_parse_stp_summary(stp_summary_text):
    s = parse_stp_summary(stp_summary_text)
    assert s.mode == "rapid-pvst" and s.portfast_default is False and s.bpduguard_default is True
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `stp.py`** with hand-written regexes: split on `^ VLAN(\d+) is executing`, read `Number of topology changes (\d+) last change occurred (\S+) ago(?: from (\S+))?`, then per `^ Port \d+ \((\S+)\) of VLAN\d+ is (\w+) (\w+)` block read `Port path cost`, the `portfast`/`edge` marker and `BPDU: sent (\d+), received (\d+)`. Summary: `Switch is in (\S+) mode`, `Portfast Default\s+is (enabled|disabled)`, `PortFast BPDU Guard Default\s+is`, `Loopguard Default\s+is`.

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(parsers): parse spanning-tree detail and summary`

---

### Task 5: MAC table and neighbour parsers (ntc-templates)

**Files:**
- Create: `src/loopwatch/parsers/tables.py`, `tests/test_parse_tables.py`

**Interfaces:**
- Produces: `parse_mac_table(text: str) -> tuple[MacEntry, ...]`, `parse_cdp_neighbors(text: str) -> tuple[Neighbor, ...]`, `parse_lldp_neighbors(text: str) -> tuple[Neighbor, ...]`, `parse_interfaces_status(text: str) -> dict[str, tuple[str, str, str]]` mapping canonical port → `(link, vlan, speed)`.
- All use `ntc_templates.parse.parse_output(platform="cisco_ios", command=..., data=text)`; results are normalised with `normalize_port` and `normalize_mac`. CDP capabilities are split on whitespace into a frozenset; LLDP capabilities come as letters (`B,R`) and are split on commas.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_mac_table(mac_text):
    macs = parse_mac_table(mac_text)
    assert MacEntry(10, "aabb.cc00.0002", "Gi1/0/14") in macs
    assert sum(1 for m in macs if m.port == "Gi1/0/48") == 40

def test_parse_cdp_neighbors(cdp_text):
    n = {x.port: x for x in parse_cdp_neighbors(cdp_text)}
    assert n["Gi1/0/48"].name.startswith("sw-core-1") and n["Gi1/0/48"].is_bridge
    assert n["Gi1/0/14"].name == "unmanaged-sw" and n["Gi1/0/14"].is_bridge

def test_parse_lldp_neighbors(lldp_text):
    n = {x.port: x for x in parse_lldp_neighbors(lldp_text)}
    assert n["Gi1/0/1"].name == "SEP001122334455" and not n["Gi1/0/1"].is_bridge

def test_parse_interfaces_status(status_text):
    st = parse_interfaces_status(status_text)
    assert st["Gi1/0/14"] == ("connected", "10", "a-1000")
    assert st["Gi1/0/48"][1] == "trunk"
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `tables.py`.** If a template is missing for a command, let `ntc_templates` raise; Task 8 turns that into `ParseError`.

- [ ] **Step 4: Run tests to verify they pass.** If a fixture does not match the template, fix the fixture to real IOS formatting, not the template.

- [ ] **Step 5: Commit** — `feat(parsers): parse mac table, cdp, lldp and interface status`

---

### Task 6: Interface counter parser

**Files:**
- Create: `src/loopwatch/parsers/interfaces.py`, `tests/test_parse_interfaces.py`

**Interfaces:**
- Produces: `parse_interfaces(text: str, status: dict[str, tuple[str, str, str]]) -> tuple[Interface, ...]`. Physical and port-channel interfaces only (names starting `Gi|Te|Twe|Fo|Hu|Fa|Po` after normalisation); `Vlan` and `Loopback` blocks are skipped. `link`, `vlan`, `speed` come from `status` when present, else `link` is derived from `line protocol is up/down` and `vlan`/`speed` are `""`. `link == "err-disabled"` when the status says `err-disabled`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_interfaces_counters(interfaces_text, status_text):
    ifs = {i.port: i for i in parse_interfaces(interfaces_text, parse_interfaces_status(status_text))}
    i14 = ifs["Gi1/0/14"]
    assert (i14.in_bcast, i14.in_mcast) == (4500, 1200)
    assert i14.in_pkts > 0 and i14.link == "connected" and i14.vlan == "10"
    assert "Vl1" not in ifs

def test_parse_interfaces_without_status(interfaces_text):
    ifs = {i.port: i for i in parse_interfaces(interfaces_text, {})}
    assert ifs["Gi1/0/14"].link == "connected" and ifs["Gi1/0/14"].vlan == ""
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement** by splitting on `^(\S+) is (up|down|administratively down), line protocol is (up|down)` and reading `(\d+) packets input`, `(\d+) packets output`, `Received (\d+) broadcasts \((\d+) multicasts\)`, `(\d+) input errors`, `(\d+) output errors` within each block.

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(parsers): parse interface counters`

---

### Task 7: Logging buffer parser

**Files:**
- Create: `src/loopwatch/parsers/logging.py`, `tests/test_parse_logging.py`

**Interfaces:**
- Produces: `parse_logging(text: str, now: datetime) -> tuple[LogLine, ...]`. Only lines containing a `%[A-Z0-9_]+-\d-[A-Z0-9_]+` mnemonic are returned. The year is taken from `now`; if the resulting time is more than one day after `now`, the previous year is used. Lines whose timestamp cannot be parsed keep `time=None`.
- `RELEVANT_MNEMONICS: tuple[str, ...]` = prefixes `SW_MATM-4-MACFLAP_NOTIF`, `SPANTREE-`, `STORM_CONTROL-`, `PM-4-ERR_DISABLE`, `LINK-3-UPDOWN`, `LINEPROTO-5-UPDOWN`, `LINK-5-CHANGED`. `is_relevant(line: LogLine) -> bool`.

- [ ] **Step 1: Write the fixture lines** into `tests/fixtures/ios15/sw-a/show_logging.txt` (the buffer section after the standard header):

```
000123: *Nov  9 09:41:58.102 CET: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/14, changed state to up
000124: *Nov  9 09:41:59.001 CET: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/14, changed state to up
000125: .Nov  9 09:42:10.500 CET: %SW_MATM-4-MACFLAP_NOTIF: Host aabb.cc00.0002 in vlan 10 is flapping between port Gi1/0/14 and port Gi1/0/48
000126: Nov  9 09:42:11 CET: %SPANTREE-5-TOPOTRAP: Topology Change Trap for vlan 10
000127: %SYS-5-CONFIG_I: Configured from console by admin on vty0
```

- [ ] **Step 2: Write the failing tests**

```python
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

def test_parse_logging_keeps_mnemonic_lines(logging_text):
    lines = parse_logging(logging_text, NOW)
    assert len(lines) == 5
    flap = next(l for l in lines if l.mnemonic == "SW_MATM-4-MACFLAP_NOTIF")
    assert flap.time == datetime(2026, 11, 9, 9, 42, 10, 500000, tzinfo=timezone.utc)
    assert "aabb.cc00.0002" in flap.text

def test_parse_logging_line_without_timestamp(logging_text):
    cfg = next(l for l in parse_logging(logging_text, NOW) if l.mnemonic == "SYS-5-CONFIG_I")
    assert cfg.time is None

def test_parse_logging_year_rollover():
    text = "000001: *Dec 31 23:59:00: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to down"
    (line,) = parse_logging(text, datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc))
    assert line.time.year == 2026

def test_is_relevant():
    assert is_relevant(LogLine(None, "SW_MATM-4-MACFLAP_NOTIF", ""))
    assert not is_relevant(LogLine(None, "SYS-5-CONFIG_I", ""))
```

Decision: timezone names in IOS logs (`CET`) are not resolved. Parse the timestamp as naive, attach UTC, and say so in the module docstring. Switch clocks should be set to UTC or the report reader must allow for the offset.

- [ ] **Step 3: Run tests to verify they fail.**

- [ ] **Step 4: Implement `logging.py`** with one regex: optional `\d+: `, optional `[*.]`, `(\w{3})\s+(\d+) (\d\d:\d\d:\d\d)(?:\.(\d+))?(?: [A-Za-z]+)?: ` then `%(\S+?): (.*)`.

- [ ] **Step 5: Run tests to verify they pass.**

- [ ] **Step 6: Commit** — `feat(parsers): parse logging buffer`

---

### Task 8: Snapshot assembly

**Files:**
- Create: `src/loopwatch/parsers/snapshot.py`, `tests/test_parse_snapshot.py`

**Interfaces:**
- Produces: `parse_snapshot(switch: str, raw: Mapping[str, str], taken_at: datetime) -> Snapshot`. Each command is parsed inside its own `try`; any exception, empty output, or output containing `% Invalid input` or `% Incomplete command` becomes `ParseError(command, message[:200])` and that section is empty (or `None` for the summary). Missing commands (skipped in inventory) produce no error. Only relevant log lines (`is_relevant`) are kept.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_snapshot_full(ios15_raw):
    s = parse_snapshot("sw-a", ios15_raw, NOW)
    assert s.parse_errors == () and len(s.stp_vlans) == 2 and len(s.neighbors) == 3
    assert all(is_relevant(l) for l in s.log_lines) and len(s.log_lines) == 4

def test_parse_snapshot_invalid_input_becomes_parse_error(ios15_raw):
    raw = dict(ios15_raw, **{"show cdp neighbors detail": "% Invalid input detected at '^' marker."})
    s = parse_snapshot("sw-a", raw, NOW)
    assert [e.command for e in s.parse_errors] == ["show cdp neighbors detail"]
    assert len(s.neighbors) == 1 and len(s.macs) > 0   # LLDP neighbour survives

def test_parse_snapshot_skipped_command_is_not_an_error(ios15_raw):
    raw = {k: v for k, v in ios15_raw.items() if k != "show cdp neighbors detail"}
    assert parse_snapshot("sw-a", raw, NOW).parse_errors == ()
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `snapshot.py`.**

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(parsers): assemble snapshot with per-command error isolation`

---

### Task 9: Store

**Files:**
- Create: `src/loopwatch/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `class Store` with `__init__(path: Path | str)` (`:memory:` allowed), `init_schema()`, `close()`, context-manager support.
  - `record_poll(switch: str, host: str, started_at, finished_at, snapshot: Snapshot | None, error: str | None) -> int` upserts the switch, inserts `polls`, and when `snapshot` is given inserts into `interfaces`, `mac_entries`, `neighbors`, `stp_vlans`, `stp_ports`, `log_lines`. Returns `poll_id`.
  - `write_events(events: Iterable[Event]) -> None`
  - `events(since: datetime, until: datetime, switch: str | None = None) -> list[Event]` ordered by time.
  - `counter_history(switch: str, since: datetime) -> dict[str, list[tuple[datetime, int]]]` port → `(finished_at, in_bcast + in_mcast)` ascending, only successful polls.
  - `latest_stp_vlans(switch: str, until: datetime) -> list[StpVlan]` and `latest_neighbors(switch: str, until: datetime) -> list[Neighbor]` from the newest successful poll at or before `until`.
  - `switches() -> list[str]`.
  - `prune(now: datetime, snapshot_days: int = 14, event_days: int = 180) -> None`.
- Schema: tables from the spec plus `stp_ports(poll_id, vlan, port, role, state, port_type, bpdu_sent, bpdu_received)`; `PRAGMA journal_mode=WAL`; indexes on `polls(switch_id, finished_at)`, `events(time)`, `interfaces(poll_id, name)`, `mac_entries(poll_id, mac)`.

- [ ] **Step 1: Write the failing tests** (an in-memory store, a parsed `ios15` snapshot from Task 8 as fixture `snap_a`)

```python
def replace_bcast(snap, port, value):   # dataclasses.replace on the matching Interface
    ...

def test_record_poll_and_counter_history(store, snap_a):
    t0 = NOW; t1 = NOW + timedelta(seconds=60)
    store.record_poll("sw-a", "10.0.0.5", t0, t0, snap_a, None)
    store.record_poll("sw-a", "10.0.0.5", t1, t1, replace_bcast(snap_a, "Gi1/0/14", 9000), None)
    hist = store.counter_history("sw-a", NOW - timedelta(hours=1))
    assert [v for _, v in hist["Gi1/0/14"]] == [5700, 10200]

def test_failed_poll_is_recorded_without_snapshot(store):
    pid = store.record_poll("sw-a", "10.0.0.5", NOW, NOW, None, "timeout")
    assert pid > 0 and store.counter_history("sw-a", NOW - timedelta(hours=1)) == {}

def test_events_roundtrip_and_filter(store): ...  # write 3 events for two switches, query by window and by switch

def test_latest_stp_vlans_and_neighbors(store, snap_a): ...  # record, then assert tc_last_port == "Gi1/0/14" and 3 neighbours

def test_prune_removes_old_snapshots_but_keeps_recent_events(store, snap_a):
    old = NOW - timedelta(days=15)
    store.record_poll("sw-a", "h", old, old, snap_a, None)
    store.write_events([Event(old, "sw-a", "mac_flap", "high", "x")])
    store.prune(NOW)
    assert store.counter_history("sw-a", NOW - timedelta(days=30)) == {}
    assert len(store.events(NOW - timedelta(days=30), NOW)) == 1
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `store.py`** with `sqlite3`, `executemany` for bulk rows, ISO strings via `datetime.isoformat()` and `datetime.fromisoformat()`.

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(store): add sqlite schema and queries`

---

### Task 10: STP and MAC-flap detectors

**Files:**
- Create: `src/loopwatch/detectors/__init__.py`, `src/loopwatch/detectors/context.py`, `src/loopwatch/detectors/stp.py`, `src/loopwatch/detectors/mac.py`, `tests/detectors/test_stp.py`, `tests/detectors/test_mac.py`, `tests/detectors/builders.py`

**Interfaces:**
- Produces, in `context.py`: `DetectorContext(prev: Snapshot | None, cur: Snapshot, baseline_pps: Mapping[str, float], recent_events: Sequence[Event], uplink_ports: frozenset[str])` and `new_log_lines(ctx) -> tuple[LogLine, ...]` = lines in `cur` whose `(time, text)` is not in `prev` (all of `cur` when `prev` is None).
- Each detector is `def detect(ctx: DetectorContext) -> list[Event]`.
- `tests/detectors/builders.py`: `make_snapshot(**overrides) -> Snapshot` with sensible defaults, `make_iface(port, **kw)`, `make_stp_vlan(vlan, **kw)`, `make_stp_port(vlan, port, **kw)`, `make_event(kind, port, minutes_ago, **kw)`.
- `stp.py`: `stp_topology_change` when `cur.tc_count > prev.tc_count` for a VLAN, `port = cur.tc_last_port`, severity `high` if `recent_events` holds ≥ 3 `stp_topology_change` for that switch in the last 5 minutes else `medium`; `stp_role_change` when `(role, state)` differs for the same `(vlan, port)`, `medium`.
- `mac.py`: `mac_flap` when a `(vlan, mac)` present in both snapshots has a different port (`port = cur port`, reason names both), and `mac_flap` for each new log line with mnemonic `SW_MATM-4-MACFLAP_NOTIF` (`mac`, `vlan`, `port` = first port in the message, reason = the line text). Severity `high`.

- [ ] **Step 1: Write the failing tests**

```python
def test_topology_change_increment_emits_event():
    prev = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=12, tc_last_port="Gi1/0/14"),))
    cur = make_snapshot(stp_vlans=(make_stp_vlan(10, tc_count=13, tc_last_port="Gi1/0/14"),))
    (ev,) = stp.detect(DetectorContext(prev, cur, {}, [], frozenset()))
    assert (ev.kind, ev.port, ev.vlan, ev.severity) == ("stp_topology_change", "Gi1/0/14", 10, "medium")

def test_topology_change_escalates_after_three_in_five_minutes():
    recent = [make_event("stp_topology_change", "Gi1/0/14", minutes_ago=m) for m in (1, 2, 3)]
    ... assert ev.severity == "high"

def test_no_prev_emits_nothing(): assert stp.detect(DetectorContext(None, make_snapshot(), {}, [], frozenset())) == []

def test_role_change(): ...  # Gi1/0/48 root/forwarding -> alternate/blocking → one stp_role_change

def test_mac_moves_between_ports():
    prev = make_snapshot(macs=(MacEntry(10, "aabb.cc00.0002", "Gi1/0/14"),))
    cur = make_snapshot(macs=(MacEntry(10, "aabb.cc00.0002", "Gi1/0/48"),))
    (ev,) = mac.detect(DetectorContext(prev, cur, {}, [], frozenset()))
    assert (ev.kind, ev.mac, ev.port, ev.severity) == ("mac_flap", "aabb.cc00.0002", "Gi1/0/48", "high")

def test_macflap_log_line_emits_event_once():
    line = LogLine(NOW, "SW_MATM-4-MACFLAP_NOTIF", "Host aabb.cc00.0002 in vlan 10 is flapping between port Gi1/0/14 and port Gi1/0/48")
    cur = make_snapshot(log_lines=(line,))
    assert len(mac.detect(DetectorContext(make_snapshot(), cur, {}, [], frozenset()))) == 1
    assert mac.detect(DetectorContext(cur, cur, {}, [], frozenset())) == []   # same line already seen

def test_new_log_lines_without_prev_returns_all(): ...
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `context.py`, `stp.py`, `mac.py`.** Event `time` is `ctx.cur.taken_at`.

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(detectors): detect topology changes, role changes and mac flaps`

---

### Task 11: Broadcast storm detector

**Files:**
- Create: `src/loopwatch/detectors/storm.py`, `tests/detectors/test_storm.py`

**Interfaces:**
- Produces: `detect(ctx) -> list[Event]`; `rate_pps(prev: Interface, cur: Interface, seconds: float) -> float | None` returns `None` when `seconds <= 0` or the counter went backwards; `baseline_from_history(history: Mapping[str, list[tuple[datetime, int]]]) -> dict[str, float]` = per port the median of consecutive positive rates (ports with fewer than 2 rates get no entry).
- `STORM_FACTOR = 20`, `STORM_MIN_PPS = 1000` as module constants with comments.
- `broadcast_storm` fires for a port when `rate is not None and rate >= STORM_MIN_PPS and rate >= STORM_FACTOR * baseline` where a missing baseline counts as `0` (so any port over 1000 pps with no history fires). Severity `high`, reason includes rate and baseline rounded to integers.

- [ ] **Step 1: Write the failing tests**

```python
def test_rate_none_when_counter_resets():
    assert rate_pps(make_iface("Gi1/0/14", in_bcast=9000), make_iface("Gi1/0/14", in_bcast=10), 60) is None

def test_storm_fires_over_factor_and_minimum():
    prev = make_snapshot(taken_at=NOW, interfaces=(make_iface("Gi1/0/14", in_bcast=0, in_mcast=0),))
    cur = make_snapshot(taken_at=NOW + timedelta(seconds=10), interfaces=(make_iface("Gi1/0/14", in_bcast=90000, in_mcast=10000),))
    (ev,) = detect(DetectorContext(prev, cur, {"Gi1/0/14": 50.0}, [], frozenset()))
    assert ev.kind == "broadcast_storm" and ev.port == "Gi1/0/14" and "10000" in ev.reason

def test_no_storm_below_minimum_even_if_factor_exceeded(): ...  # baseline 10, rate 500 → []
def test_no_storm_when_within_factor(): ...  # baseline 1000, rate 5000 → []
def test_counter_reset_does_not_fire(): ...  # prev 1_000_000, cur 100 → []

def test_baseline_from_history_is_median_of_rates():
    h = {"Gi1/0/1": [(NOW, 0), (NOW + timedelta(seconds=60), 600), (NOW + timedelta(seconds=120), 1800), (NOW + timedelta(seconds=180), 2400)]}
    assert baseline_from_history(h) == {"Gi1/0/1": 10.0}   # rates 10, 20, 10 → median 10
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `storm.py`** using `statistics.median`.

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(detectors): detect broadcast storms against a per-port baseline`

---

### Task 12: Edge, link and err-disable detectors and the runner

**Files:**
- Create: `src/loopwatch/detectors/edge.py`, `src/loopwatch/detectors/links.py`, `src/loopwatch/detectors/runner.py`, `tests/detectors/test_edge.py`, `tests/detectors/test_links.py`, `tests/detectors/test_runner.py`

**Interfaces:**
- `edge.py` `detect(ctx)`: `unexpected_neighbor` (high) when a `Neighbor.is_bridge` sits on a port whose `StpPort.port_type == "edge"` in any VLAN and that neighbour was not present on that port in `prev` (or `prev` is None); and when an edge port's `bpdu_received` increased since `prev`. Reason names the neighbour or the BPDU count.
- `links.py` `detect(ctx)`: `link_flap` when `prev.link == "connected"` and `cur.link == "notconnect"` or the reverse, `low`; `medium` when `recent_events` holds ≥ 3 `link_flap` for that port in 5 minutes or new log lines hold ≥ 3 `LINK-3-UPDOWN` for that port. `err_disable` (high) when `cur.link == "err-disabled"` and `prev.link != "err-disabled"`, or a new log line has mnemonic `PM-4-ERR_DISABLE`; reason includes the log text when present.
- `runner.py`: `ALL_DETECTORS: tuple[Callable[[DetectorContext], list[Event]], ...]` in order stp, mac, storm, edge, links; `run_detectors(ctx) -> list[Event]` concatenates and additionally emits one `parse_error` (`low`) event per `cur.parse_errors` entry.

- [ ] **Step 1: Write the failing tests**

```python
def test_bridge_on_edge_port_is_unexpected():
    cur = make_snapshot(stp_ports=(make_stp_port(10, "Gi1/0/14", port_type="edge"),),
                        neighbors=(Neighbor("Gi1/0/14", "unmanaged-sw", "1", "SG250", frozenset({"S"})),))
    (ev,) = edge.detect(DetectorContext(None, cur, {}, [], frozenset()))
    assert ev.kind == "unexpected_neighbor" and ev.port == "Gi1/0/14"

def test_bridge_on_network_port_is_fine(): ...  # port_type "network" → []
def test_same_bridge_already_present_is_not_repeated(): ...  # prev has the same neighbour → []
def test_bpdu_increase_on_edge_port(): ...  # bpdu_received 0 → 3 → one event

def test_link_flap_low_then_medium(): ...
def test_err_disable_from_status(): ...  # connected → err-disabled → high
def test_err_disable_from_log(): ...  # new PM-4-ERR_DISABLE line → high

def test_run_detectors_adds_parse_error_events():
    cur = make_snapshot(parse_errors=(ParseError("show cdp neighbors detail", "boom"),))
    evs = run_detectors(DetectorContext(None, cur, {}, [], frozenset()))
    assert [e.kind for e in evs] == ["parse_error"]
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement the three modules.**

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(detectors): detect unexpected neighbours, link flaps and err-disable; add runner`

---

### Task 13: Suspect ranking and topology-change chain

**Files:**
- Create: `src/loopwatch/report/__init__.py`, `src/loopwatch/report/ranking.py`, `tests/report/test_ranking.py`

**Interfaces:**
- Produces: `Suspect(switch: str, port: str, score: int, events: tuple[Event, ...])`; `rank_suspects(events: Sequence[Event], uplinks: AbstractSet[tuple[str, str]]) -> list[Suspect]` sorted by score descending, top 10, using the spec weights: `unexpected_neighbor` +50, `err_disable` +50, `mac_flap` +20 per distinct MAC capped at 100, `broadcast_storm` +30, `stp_topology_change` +10 each capped at 50; score halved (integer division) when `(switch, port)` in `uplinks`. Events without a port are ignored.
- `uplinks_from_neighbors(neighbors_by_switch: Mapping[str, Sequence[Neighbor]], managed: AbstractSet[str]) -> set[tuple[str, str]]`: a port is an uplink when its neighbour `name` (before the first `.`) is a managed switch name.
- `ChainHop(switch: str, port: str, next_switch: str | None)`; `tc_chain(vlan: int, stp_by_switch: Mapping[str, Sequence[StpVlan]], neighbors_by_switch: Mapping[str, Sequence[Neighbor]], start: str) -> list[ChainHop]`: from `start`, follow `tc_last_port` to the neighbour on that port while the neighbour is managed and not yet visited; stops after 32 hops.

- [ ] **Step 1: Write the failing tests**

```python
def test_rank_suspects_weights_and_cap():
    evs = [make_event("mac_flap", "Gi1/0/14", 1, mac=f"aabb.cc00.{i:04x}") for i in range(8)]  # 8 MACs → capped 100
    evs += [make_event("unexpected_neighbor", "Gi1/0/14", 1)]
    evs += [make_event("stp_topology_change", "Gi1/0/48", 1)] * 7   # 70 → capped 50
    top = rank_suspects(evs, uplinks={("sw-a", "Gi1/0/48")})
    assert (top[0].port, top[0].score) == ("Gi1/0/14", 150)
    assert (top[1].port, top[1].score) == ("Gi1/0/48", 25)

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
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `ranking.py`.**

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(report): rank suspect ports and follow the topology-change chain`

---

### Task 14: Report rendering

**Files:**
- Create: `src/loopwatch/report/render.py`, `src/loopwatch/report/templates/report.html.j2`, `tests/report/test_render.py`

**Interfaces:**
- Produces: `ReportData(window: tuple[datetime, datetime], suspects: list[Suspect], events: list[Event], chains: dict[int, list[ChainHop]])`; `build_report(store: Store, managed: AbstractSet[str], since: datetime, until: datetime) -> ReportData` (chains built per VLAN that has any `stp_topology_change` in the window, starting from the switch with the highest `tc_count`); `render_text(data) -> str`; `render_html(data) -> str` via Jinja2 `PackageLoader("loopwatch.report", "templates")` with autoescape on.
- Text layout: a header with the window, a "Suspects" table (rank, switch, port, score, one-line summary of event kinds), a "Topology change chain" block per VLAN as `sw-core-1 Gi1/0/1 -> sw-a Gi1/0/14 -> (unmanaged)`, then a "Timeline" with one event per line `HH:MM:SS switch kind sev port vlan mac reason`.

- [ ] **Step 1: Write the failing tests** (store seeded with the Task 9 fixtures plus a handful of events)

```python
def test_build_report_ranks_and_chains(seeded_store):
    data = build_report(seeded_store, {"sw-a", "sw-core-1"}, NOW - timedelta(hours=1), NOW)
    assert data.suspects[0].port == "Gi1/0/14"
    assert 10 in data.chains

def test_render_text_contains_sections(report_data):
    text = render_text(report_data)
    assert "Suspects" in text and "Gi1/0/14" in text and "Timeline" in text and "->" in text

def test_render_html_escapes_and_lists_events(report_data_with_angle_bracket_reason):
    html = render_html(report_data_with_angle_bracket_reason)
    assert "&lt;script&gt;" in html and "<script>" not in html
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `render.py` and the template** (single page, inline CSS, no external assets, table per section). Add `[tool.hatch.build] include` so `templates/*.j2` ships in the package.

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit** — `feat(report): render text and html reports`

---

### Task 15: Collector

**Files:**
- Create: `src/loopwatch/collector.py`, `tests/test_collector.py`, `tests/fixtures/ios15/sw-core-1/*.txt` (eight files for a second switch: hostname `sw-core-1`, CDP neighbour `sw-a` on Gi1/0/1, VLAN0010 `tc_count 5` from Gi1/0/1)

**Interfaces:**
- Produces: `poll_once(switch: Switch, inv: Inventory, transport: Transport, store: Store, prev: Snapshot | None, now: datetime) -> tuple[Snapshot | None, list[Event]]`: runs transport, parses, builds `DetectorContext` (baseline from `store.counter_history(switch.name, now - 1h)` via `baseline_from_history`; `recent_events` = `store.events(now - 5min, now, switch.name)`; `uplink_ports` from the previous snapshot's neighbours and `inv` names), runs detectors, records the poll and events. On `TransportError` records the failed poll and returns `(None, [Event(... "unreachable", "low" ...)])`; severity `medium` when `store.events(now - 2min, now)` already holds an `unreachable` for another switch.
- `class Escalation`: `trigger(switch: str, neighbors: Iterable[str], now)` sets `until = now + 600s` for the switch and the named neighbours; `interval(switch: str, default: int, now) -> int` returns 10 while escalated else `default`.
- `async def run_collector(inv: Inventory, transport: Transport, store: Store, *, clock: Callable[[], datetime] = utcnow, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, stop: asyncio.Event, max_sessions: int = 8) -> None`: one task per switch; each loop iteration awaits a `Semaphore(max_sessions)`, calls `poll_once` in `asyncio.to_thread`, triggers escalation when any event has severity `high` or kind `stp_topology_change`, then sleeps `Escalation.interval`. A separate task calls `store.prune(clock())` once per 24 h. Returns when `stop` is set.
- Logging: one `INFO` line per poll `poll switch=%s ok=%s events=%d duration=%.1fs`, through `logging.getLogger("loopwatch")`.
- Only `TransportError` is caught in `poll_once`. `sqlite3.Error` and any other exception propagate out of the switch task and end `run_collector` with the exception, so a broken database never fails silently. Test: `test_store_error_stops_collector` makes `store.record_poll` raise `sqlite3.OperationalError` and asserts `run_collector` raises it.

- [ ] **Step 1: Write the failing tests**

```python
def test_poll_once_first_poll_stores_snapshot_and_unexpected_neighbor(store, inv, fake):
    snap, events = poll_once(inv.by_name("sw-a"), inv, fake, store, None, NOW)
    # no prev: the bridge on edge port Gi1/0/14 fires, and the MACFLAP line in the log buffer counts as new
    assert snap is not None and {e.kind for e in events} == {"unexpected_neighbor", "mac_flap"}
    assert len(store.events(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))) == 2

def test_poll_once_unreachable(store, inv, unreachable_fake):
    snap, events = poll_once(inv.by_name("sw-a"), inv, unreachable_fake, store, None, NOW)
    assert snap is None and events[0].kind == "unreachable" and events[0].severity == "low"

def test_escalation_interval():
    e = Escalation(); e.trigger("sw-a", ["sw-core-1"], NOW)
    assert e.interval("sw-a", 60, NOW + timedelta(seconds=599)) == 10
    assert e.interval("sw-core-1", 60, NOW) == 10
    assert e.interval("sw-a", 60, NOW + timedelta(seconds=601)) == 60
    assert e.interval("sw-b", 60, NOW) == 60

async def test_run_collector_polls_each_switch_and_stops(store, inv, fake):
    stop = asyncio.Event(); sleeps: list[float] = []
    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 4:
            stop.set()
    await run_collector(inv, fake, store, clock=lambda: NOW, sleep=fake_sleep, stop=stop)
    assert {c[0] for c in fake.calls} == {"sw-a", "sw-core-1"}
    assert 10 in sleeps   # sw-a's unexpected_neighbor escalated it
```

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `collector.py`.** Use `asyncio.wait` with the stop event so sleeping tasks wake on stop.

- [ ] **Step 4: Run tests to verify they pass** (`pytest -q`, with `asyncio_mode = "auto"` added to pytest config, which needs `pytest-asyncio` in dev deps; add it).

- [ ] **Step 5: Commit** — `feat(collector): schedule polls with escalation and pruning`

---

### Task 16: CLI, Docker and README

**Files:**
- Create: `src/loopwatch/cli.py`, `tests/test_cli.py`, `Dockerfile`, `docker-compose.yml`, `inventory.example.yaml`
- Modify: `README.md`

**Interfaces:**
- Produces `main` (click group) with options shared via environment: `--inventory PATH` (default `inventory.yaml`, env `LOOPWATCH_INVENTORY`), `--db PATH` (default `loopwatch.sqlite`, env `LOOPWATCH_DB`). Credentials read from `os.environ`.
  - `watch`: runs `run_collector` with `NetmikoTransport` until SIGINT/SIGTERM.
  - `report [--since 2h | --from ISO --to ISO] [--html PATH]`: text to stdout, or HTML written to PATH. `--since` accepts `Nm|Nh|Nd`.
  - `snapshot [SWITCH...] [--record DIR]`: polls the named switches (all when none) once, concurrently, through `poll_once`; with `--record`, also writes raw output to `DIR/<switch>/<command_slug>.txt`; then prints `render_text` for the last 15 minutes.
  - `check-inventory`: connects to each switch, runs `show version`, prints `ok`/`FAIL <message>` per switch, exit code 1 if any failed.
  - Hidden option `--transport-fixtures DIR` on `snapshot` and `check-inventory` that swaps in `FakeTransport(DIR)`, so the CLI is testable.
- `Dockerfile`: `python:3.12-slim`, copies `pyproject.toml` and `src`, `pip install .`, `CMD ["loop-watch", "watch"]`. `docker-compose.yml`: service `loop-watch`, `env_file: .env`, volume `./inventory.yaml:/app/inventory.yaml:ro`, named volume `loopwatch-data:/data`, env `LOOPWATCH_INVENTORY=/app/inventory.yaml`, `LOOPWATCH_DB=/data/loopwatch.sqlite`, `restart: unless-stopped`.

- [ ] **Step 1: Write the failing tests** (click `CliRunner`, fixtures dir as transport)

```python
def test_snapshot_with_fixtures_prints_suspects(tmp_path, fixtures_root, inventory_file, monkeypatch):
    monkeypatch.setenv("LOOPWATCH_SSH_USER", "ro"); monkeypatch.setenv("LOOPWATCH_SSH_PASSWORD", "hunter2")
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "--db", str(tmp_path / "db.sqlite"),
                                  "snapshot", "--transport-fixtures", str(fixtures_root), "--record", str(tmp_path / "rec")])
    assert r.exit_code == 0, r.output
    assert "Gi1/0/14" in r.output
    assert (tmp_path / "rec" / "sw-a" / "show_spanning-tree_detail.txt").exists()

def test_report_since_parses_and_prints(tmp_path, seeded_db_path):
    r = CliRunner().invoke(main, ["--db", str(seeded_db_path), "report", "--since", "2h"])
    assert r.exit_code == 0 and "Suspects" in r.output

def test_report_html_writes_file(tmp_path, seeded_db_path): ...

def test_check_inventory_reports_failure(tmp_path, inventory_file, fixtures_root_with_unreachable, monkeypatch):
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "check-inventory", "--transport-fixtures", str(fixtures_root_with_unreachable)])
    assert r.exit_code == 1 and "FAIL" in r.output

def test_output_never_contains_password(...):  # LOOPWATCH_SSH_PASSWORD="hunter2", snapshot with an UNREACHABLE fixture; assert "hunter2" not in r.output
```

For `check-inventory` with fixtures, `FakeTransport` must serve `show_version.txt`; add that file to both fixture switch directories.

- [ ] **Step 2: Run tests to verify they fail.**

- [ ] **Step 3: Implement `cli.py`, `Dockerfile`, `docker-compose.yml`, `inventory.example.yaml`.**

- [ ] **Step 4: Run tests to verify they pass; build the image**

Run: `pytest -q` — all pass. Run: `docker build -t loop-watch .` — Expected: image builds. Run: `docker run --rm loop-watch loop-watch --help` — Expected: usage text with the four commands.

- [ ] **Step 5: Write `README.md`**: purpose, requirements on the switch account (read-only, `show` commands only), setup (`cp .env.example .env`, `cp inventory.example.yaml inventory.yaml`, `docker compose up -d`), the four commands with one example each, how to record real fixtures with `snapshot --record` and anonymise them before committing, and where thresholds live.

- [ ] **Step 6: Commit**

```bash
git add src/loopwatch/cli.py tests/test_cli.py tests/fixtures Dockerfile docker-compose.yml inventory.example.yaml README.md pyproject.toml
git commit -m "feat(cli): add watch, report, snapshot and check-inventory commands with docker packaging"
```

---

### Task 17: Second IOS fixture set and end-to-end scenario

**Files:**
- Create: `tests/fixtures/iosxe17/sw-9300/*.txt` (nine files, IOS-XE 17.x Catalyst 9300 formatting: `TwentyFiveGigE1/1/1` uplink, `show interfaces status` with `a-full` duplex column, `show logging` lines with `.Nov` prefixes), `tests/test_scenario.py`, `tests/fixtures/scenario/{t0,t1,t2}/sw-a/*.txt`

**Interfaces:** none new.

- [ ] **Step 1: Write the IOS-XE fixtures and parametrise the parser tests** of Tasks 4–8 over both fixture sets where the asserted values are the same shape (vlan count, one edge port, one uplink); where values differ, assert per set with a small expected-values dict in `tests/conftest.py`.

- [ ] **Step 2: Write the scenario fixtures**: `t0` = the baseline `ios15/sw-a` output with no neighbour on Gi1/0/14 and `tc_count 12`; `t1` = `unmanaged-sw` appears on Gi1/0/14, `bpdu_received 3`, `tc_count 13`; `t2` = MAC `aabb.cc00.0002` moved to Gi1/0/48, MACFLAP log line present, Gi1/0/14 broadcasts up by 600 000 over 60 s.

- [ ] **Step 3: Write the failing scenario test**

```python
def test_desk_switch_scenario_ranks_gi14_first(tmp_path, inv):
    store = Store(":memory:"); store.init_schema(); prev = None
    for i, t in enumerate(("t0", "t1", "t2")):
        prev, _ = poll_once(inv.by_name("sw-a"), inv, FakeTransport(SCENARIO / t), store, prev, NOW + timedelta(seconds=60 * i))
    data = build_report(store, {"sw-a", "sw-core-1"}, NOW, NOW + timedelta(minutes=5))
    kinds = {e.kind for e in data.events}
    assert {"unexpected_neighbor", "stp_topology_change", "mac_flap", "broadcast_storm"} <= kinds
    assert data.suspects[0].port == "Gi1/0/14"
```

- [ ] **Step 4: Run tests to verify they fail, fix parsers or fixtures until they pass** (`pytest -q`).

- [ ] **Step 5: Commit** — `test: add ios-xe fixtures and desk-switch loop scenario`
