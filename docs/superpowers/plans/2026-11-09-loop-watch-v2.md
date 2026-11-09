# loop-watch v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add device profiles with MikroTik RouterOS support, credential sets, a live-reloading inventory, an aiohttp web UI with device editing, a culprit diagnosis (port, device, MACs with hostname and vendor), local time rendering, and a published container image.

**Architecture:** One process: `watch` runs the collector and the aiohttp server in one asyncio loop sharing one `Store` and one inventory file on the data volume. A `profiles` module maps `device_type` to commands and a parse function, so parsers and detectors keep taking plain data. The diagnosis is a pure function over stored events, MAC tables, neighbours and DHCP hosts.

**Tech Stack:** Python 3.12, netmiko, ntc-templates, PyYAML, click, Jinja2, aiohttp (new), netaddr (new); pytest, pytest-asyncio, pytest-aiohttp (new, dev); GitHub Actions, ghcr.io.

**Spec:** `docs/superpowers/specs/2026-11-09-loop-watch-v2-design.md`

## Global Constraints

- Python `>=3.12`; the suite must pass on the host and inside `python:3.12-slim`.
- No test opens a network connection or needs Docker.
- Credentials never appear in the inventory file, the database, log output, reports or pages. `Credentials.__repr__` stays redacting.
- `parsers` and `detectors` import nothing from `transport`, `store`, `collector`, `web` or `cli`.
- Storage and comparisons stay UTC; only rendering converts to `LOOPWATCH_TZ`.
- Conventional Commits, one logical change per commit, every commit green.
- Port names: Cisco short form via `normalize_port`; RouterOS names unchanged. MACs lowercase dotted via `normalize_mac`.
- A command the parser cannot read becomes a `parse_error` event, never an exception out of `parse_snapshot`.
- Deviation from the spec, decided here: hosts come from DHCP leases only. `Neighbor` has no management address field, so the "lldp:<device>" host source is left out of this version.

## Review Focus

1. A v1 database opened by v2: `init_schema` must add `events.other_port` and create `hosts` without touching existing rows; `events()` must read rows where `other_port` is NULL. Test in Task 5.
2. RouterOS `print stats` with more interfaces than fit one block: values must join to the right interface across blocks. Test in Task 6.
3. Inventory saved by the web form and read back by the collector while a poll is in flight: the diff must not restart an unchanged device and must not crash on a half-written file (atomic replace). Tests in Tasks 3 and 7.
4. A POST from another origin (hostile LAN page) to `/devices/x/delete` must be refused with 403 and change nothing. Test in Task 10.
5. Diagnosis with no MAC table on the suspect switch (router as top suspect) or no DHCP hosts: every list is empty, no exception, the text says what is unknown. Test in Task 9.

---

### Task 1: CI and image publishing

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `.dockerignore`

**Interfaces:**
- Produces: image `ghcr.io/patrickb100/loop-watch:{latest,sha-<7>,<semver>}`.

- [ ] **Step 1: Write `.dockerignore`**

Lines: `.git`, `.venv`, `tests`, `docs`, `.env*`, `*.sqlite*`, `__pycache__`, `.superpowers`, `.github`.

- [ ] **Step 2: Write `ci.yml`**

Trigger `push` (all branches) and `pull_request`. One job on `ubuntu-latest`: `actions/checkout@v4`, `actions/setup-python@v5` with `python-version: "3.12"`, `pip install .[dev]`, `pytest -q`.

- [ ] **Step 3: Write `publish.yml`**

Trigger `push` to `main` and tags `v*`. Job `test` as in ci.yml. Job `image` needs `test`, `permissions: {contents: read, packages: write}`: checkout, `docker/login-action@v3` to `ghcr.io` with `${{ github.actor }}` / `${{ secrets.GITHUB_TOKEN }}`, `docker/metadata-action@v5` on `ghcr.io/${{ github.repository }}` with tags `type=raw,value=latest,enable={{is_default_branch}}`, `type=sha,prefix=sha-`, `type=semver,pattern={{version}}`; `docker/build-push-action@v6` with `push: true`, the metadata tags and labels.

- [ ] **Step 4: Verify the YAML parses and the image builds locally**

Run: `.venv/bin/python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]; print('ok')" .github/workflows/ci.yml .github/workflows/publish.yml && docker build -q -t loop-watch:plan .`
Expected: `ok` and an image id. (Skip the docker part and say so if Docker is not running.)

- [ ] **Step 5: Commit**

```bash
git add .github .dockerignore
git commit -m "ci: run tests on push and publish the image to ghcr.io from main"
```

---

### Task 2: Device profiles

**Files:**
- Create: `src/loopwatch/profiles.py`, `tests/test_profiles.py`
- Modify: `src/loopwatch/inventory.py` (`Switch.commands`), `src/loopwatch/cli.py` (`check_inventory`)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Profile:
      family: str
      device_types: frozenset[str]
      commands: tuple[str, ...]
      parse: Callable[[str, Mapping[str, str], datetime], Snapshot]
      version_command: str
      version_pattern: str
  CISCO_IOS: Profile            # family "cisco_ios", types {"cisco_ios","cisco_xe"}, inventory.COMMANDS, parsers.snapshot.parse_snapshot, "show version", r"Version ([^,\s]+)"
  PROFILES: tuple[Profile, ...]
  def profile_for(device_type: str) -> Profile   # raises InventoryError
  def version_of(profile: Profile, text: str) -> str | None
  ```
- Consumes: `inventory.COMMANDS`, `InventoryError`, `parsers.snapshot.parse_snapshot`.

Note: `profiles` imports `inventory` for `COMMANDS` and `InventoryError`; `inventory.Switch.commands` imports `profiles` lazily inside the property to avoid the cycle. Task 6 appends the MikroTik profile.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_profiles.py
import pytest
from loopwatch.inventory import COMMANDS, InventoryError, Switch
from loopwatch.profiles import CISCO_IOS, profile_for, version_of

def test_cisco_types_share_one_profile():
    assert profile_for("cisco_ios") is CISCO_IOS and profile_for("cisco_xe") is CISCO_IOS
    assert CISCO_IOS.commands == COMMANDS

def test_unknown_device_type_raises():
    with pytest.raises(InventoryError, match="unknown device_type 'junos'"):
        profile_for("junos")

def test_switch_commands_follow_profile_minus_skip():
    sw = Switch(name="a", host="h", skip=frozenset({"show logging"}))
    assert sw.commands == tuple(c for c in COMMANDS if c != "show logging")

def test_version_of_cisco():
    assert version_of(CISCO_IOS, "Cisco IOS Software, Version 15.2(7)E8, RELEASE") == "15.2(7)E8"
    assert version_of(CISCO_IOS, "garbage") is None
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/pytest tests/test_profiles.py -q`
Expected: FAIL, `ModuleNotFoundError: loopwatch.profiles`.

- [ ] **Step 3: Implement `profiles.py`; make `Switch.commands` use `profile_for(self.device_type).commands`; make `check_inventory` use `profile.version_command` and `version_of`**

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (171 + 4).

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/profiles.py src/loopwatch/inventory.py src/loopwatch/cli.py tests/test_profiles.py
git commit -m "refactor(kit): introduce device profiles for commands, parsing and version"
```

---

### Task 3: Credential sets, `ap_ports`, writable inventory

**Files:**
- Modify: `src/loopwatch/inventory.py`, `src/loopwatch/collector.py` (`poll_once` uses `inv.credentials_for`), `src/loopwatch/cli.py` (`check_inventory`, `_managed_names`), `tests/conftest.py`, `tests/test_inventory.py`, `tests/test_cli.py`
- Create: `tests/fixtures/inventory/sets.yaml`

**Interfaces:**
- Produces:
  ```python
  class Switch:  # new fields, both with defaults
      credentials: str = "ssh"
      ap_ports: frozenset[str] = frozenset()
  class Inventory:
      switches: tuple[Switch, ...]
      credential_sets: Mapping[str, Credentials]
      defaults: Mapping[str, object]          # as read from the file, for save round-trips
      def credentials_for(self, switch: Switch) -> Credentials   # KeyError never: validated at load
      def ap_ports(self) -> frozenset[tuple[str, str]]           # (switch name, port)
  def credential_sets_from_env(env: Mapping[str, str]) -> dict[str, Credentials]
  def load_inventory(path: Path, env: Mapping[str, str]) -> Inventory  # missing file -> empty inventory
  def validate_device(switch: Switch, credential_sets: Set[str]) -> list[str]
  def save_inventory(path: Path, switches: Sequence[Switch], defaults: Mapping[str, object]) -> None
  ENV_SET = re.compile(r"^LOOPWATCH_([A-Z0-9]+)_USER$")
  ```
- Consumes: `profiles.profile_for` (for `skip` validation), `ports.normalize_port` (for `ap_ports`).

Breaking change to `Inventory(...)`: positional `(switches, credentials)` becomes `(switches, credential_sets, defaults={})`. Update the `inv` fixture in `tests/conftest.py` to `Inventory((sw_a, Switch(name="sw-core-1", host="10.0.0.1")), {"ssh": creds})`. `check_inventory` and `snapshot` in the CLI call `transport.run(sw, inv.credentials_for(sw), ...)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_inventory.py`)

```python
def test_credential_sets_from_env_names_pairs_only():
    env = {"LOOPWATCH_SSH_USER": "a", "LOOPWATCH_SSH_PASSWORD": "b", "LOOPWATCH_SSH_ENABLE": "e",
           "LOOPWATCH_MIKROTIK_USER": "m", "LOOPWATCH_MIKROTIK_PASSWORD": "n",
           "LOOPWATCH_LAB_USER": "no-password", "OTHER": "x"}
    sets = credential_sets_from_env(env)
    assert set(sets) == {"ssh", "mikrotik"}
    assert sets["ssh"].enable == "e" and sets["mikrotik"].enable is None

def test_load_resolves_credential_set_and_ap_ports(tmp_path):
    p = tmp_path / "i.yaml"
    p.write_text("switches:\n  - {name: ccr, host: 10.0.0.254, device_type: mikrotik_routeros, credentials: mikrotik}\n"
                 "  - {name: sw-a, host: 10.0.0.5, ap_ports: [GigabitEthernet1/0/20, Gi1/0/21]}\n"
                 "defaults: {credentials: ssh}\n")
    env = {"LOOPWATCH_SSH_USER": "a", "LOOPWATCH_SSH_PASSWORD": "b",
           "LOOPWATCH_MIKROTIK_USER": "m", "LOOPWATCH_MIKROTIK_PASSWORD": "n"}
    inv = load_inventory(p, env)
    assert inv.credentials_for(inv.by_name("ccr")).user == "m"
    assert inv.credentials_for(inv.by_name("sw-a")).user == "a"
    assert inv.ap_ports() == frozenset({("sw-a", "Gi1/0/20"), ("sw-a", "Gi1/0/21")})

def test_load_rejects_unknown_credential_set(tmp_path):
    p = tmp_path / "i.yaml"
    p.write_text("switches:\n  - {name: x, host: h, credentials: lab}\n")
    with pytest.raises(InventoryError, match="credential set 'lab'.*LOOPWATCH_LAB_USER"):
        load_inventory(p, {"LOOPWATCH_SSH_USER": "a", "LOOPWATCH_SSH_PASSWORD": "b"})

def test_missing_file_is_empty_inventory(tmp_path):
    inv = load_inventory(tmp_path / "none.yaml", {})
    assert inv.switches == () and inv.credential_sets == {}

def test_validate_device_messages():
    sets = {"ssh"}
    assert validate_device(Switch(name="ok-1", host="10.0.0.1"), sets) == []
    msgs = validate_device(Switch(name="bad name", host="10.0.0.1 x", device_type="junos", poll_interval=5,
                                  skip=frozenset({"show foo"}), credentials="lab"), sets)
    assert any("name" in m.lower() for m in msgs)
    assert any("host" in m.lower() for m in msgs)
    assert any("device type" in m.lower() for m in msgs)
    assert any("10 and 3600" in m for m in msgs)
    assert any("show foo" in m for m in msgs)
    assert any("'lab'" in m for m in msgs)

def test_save_then_load_round_trip(tmp_path):
    p = tmp_path / "i.yaml"
    env = {"LOOPWATCH_SSH_USER": "a", "LOOPWATCH_SSH_PASSWORD": "b", "LOOPWATCH_MIKROTIK_USER": "m", "LOOPWATCH_MIKROTIK_PASSWORD": "n"}
    switches = (Switch(name="sw-a", host="10.0.0.5", skip=frozenset({"show logging"}), ap_ports=frozenset({"Gi1/0/20"})),
                Switch(name="ccr", host="10.0.0.254", device_type="mikrotik_routeros", credentials="mikrotik", poll_interval=30))
    save_inventory(p, switches, {"device_type": "cisco_ios", "poll_interval": 60})
    assert "LOOPWATCH" not in p.read_text() and "b" != p.read_text()
    assert load_inventory(p, env).switches == switches
    assert not list(tmp_path.glob("*.tmp*"))  # temp file replaced, not left behind
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/pytest tests/test_inventory.py -q`
Expected: FAIL, `ImportError` on `credential_sets_from_env`.

- [ ] **Step 3: Implement**

`validate_device` messages: `"Name must be 1-63 characters: letters, digits, dot, dash, underscore"`, `"Host must not contain whitespace"`, `"Unknown device type 'junos'"`, `"Poll interval must be between 10 and 3600 seconds"`, `"'show foo' is not a command of this device type"`, `"Credential set 'lab' is not defined (LOOPWATCH_LAB_USER / LOOPWATCH_LAB_PASSWORD)"`. `save_inventory`: `yaml.safe_dump` with `sort_keys=False`, omitting fields equal to defaults; write to `path.with_suffix(".yaml.tmp")` then `os.replace`. `load_inventory` runs `validate_device` on each entry and raises `InventoryError` with the joined messages.

- [ ] **Step 4: Update callers and fixtures; run the suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/inventory.py src/loopwatch/collector.py src/loopwatch/cli.py tests
git commit -m "feat(inventory): credential sets from the environment, ap_ports, validation and saving"
```

---

### Task 4: Transport without `terminal length 0`

**Files:**
- Modify: `src/loopwatch/transport.py`, `tests/test_transport.py`

- [ ] **Step 1: Write the failing test**

```python
def test_netmiko_transport_sends_only_the_requested_commands(monkeypatch, sw_a, creds):
    import sys, types
    sent = []
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def enable(self): pass
        def send_command(self, cmd, **kw): sent.append(cmd); return f"out of {cmd}"
    fake = types.ModuleType("netmiko"); fake.ConnectHandler = lambda **kw: FakeConn()
    monkeypatch.setitem(sys.modules, "netmiko", fake)
    out = NetmikoTransport().run(sw_a, creds, ["show version"])
    assert sent == ["show version"] and out == {"show version": "out of show version"}
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_transport.py -q -k only_the_requested`
Expected: FAIL, `sent == ["terminal length 0", "show version"]`.

- [ ] **Step 3: Remove the `terminal length 0` call** (netmiko's Cisco and MikroTik drivers disable paging in `session_preparation`)

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/transport.py tests/test_transport.py
git commit -m "fix(transport): drop terminal length 0, which RouterOS rejects and netmiko already handles"
```

---

### Task 5: Model and store additions

**Files:**
- Modify: `src/loopwatch/model.py`, `src/loopwatch/store.py`, `src/loopwatch/detectors/mac.py`, `tests/test_store.py`, `tests/detectors/test_mac.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Host: mac: str; ip: str | None; hostname: str | None; active: bool
  class Snapshot: hosts: tuple[Host, ...] = ()          # appended, default empty
  class Event: other_port: str | None = None           # appended
  class Store:
      def upsert_hosts(self, hosts: Iterable[Host], source: str, seen: datetime) -> None
      def hosts_for(self, macs: Set[str]) -> dict[str, Host]
      def log_lines(self, since: datetime, until: datetime) -> list[tuple[datetime, str, LogLine]]  # (polled at, switch, line)
      def last_poll(self, switch: str) -> tuple[datetime, bool, str | None] | None   # finished_at, ok, error
      def mac_ports(self, switch: str, since: datetime, until: datetime) -> dict[str, set[str]]  # mac -> ports seen
  ```
- Schema: `hosts(mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, source TEXT, seen_at TEXT)`; `events.other_port TEXT` added via `ALTER TABLE` when `PRAGMA table_info(events)` lacks it. `prune` deletes hosts with `seen_at < event_cutoff`. `events()` reads and `write_events` writes `other_port`. `latest_snapshot` returns `hosts=()`.
- MAC detector: table-based flap sets `other_port=old_port`; log-based sets `other_port=normalize_port(m.group(4))` when the port named in `port=` is group 3 (so `other_port` is the other one).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store.py additions
def test_v1_schema_is_upgraded_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "v1.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE switches (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, host TEXT NOT NULL, model TEXT, last_seen TEXT);
      CREATE TABLE events (id INTEGER PRIMARY KEY, time TEXT NOT NULL, switch_id INTEGER NOT NULL REFERENCES switches(id),
      kind TEXT NOT NULL, severity TEXT NOT NULL, port TEXT, vlan INTEGER, mac TEXT, reason TEXT NOT NULL);
      INSERT INTO switches(name, host) VALUES ('sw-a', 'h');
      INSERT INTO events(time, switch_id, kind, severity, reason) VALUES ('2026-11-09T09:00:00+00:00', 1, 'mac_flap', 'high', 'old');""")
    con.commit(); con.close()
    with Store(path) as s:
        s.init_schema()
        evs = s.events(NOW - timedelta(days=1), NOW)
        assert len(evs) == 1 and evs[0].other_port is None
        s.write_events([Event(NOW, "sw-a", "mac_flap", "high", "r", port="Gi1/0/14", mac="aaaa.bbbb.cccc", other_port="Gi1/0/7")])
        assert s.events(NOW - timedelta(days=1), NOW)[-1].other_port == "Gi1/0/7"

def test_hosts_upsert_latest_wins_and_prunes(store):
    store.upsert_hosts([Host("aaaa.bbbb.cccc", "10.0.0.9", "laptop", True)], "dhcp:ccr", NOW - timedelta(days=200))
    store.upsert_hosts([Host("aaaa.bbbb.cccc", "10.0.0.10", "laptop", True)], "dhcp:ccr", NOW)
    assert store.hosts_for({"aaaa.bbbb.cccc"})["aaaa.bbbb.cccc"].ip == "10.0.0.10"
    store.upsert_hosts([Host("1111.2222.3333", None, None, False)], "dhcp:ccr", NOW - timedelta(days=200))
    store.prune(NOW)
    assert set(store.hosts_for({"aaaa.bbbb.cccc", "1111.2222.3333"})) == {"aaaa.bbbb.cccc"}

def test_log_lines_last_poll_and_mac_ports(store, snap_a):
    store.record_poll("sw-a", "10.0.0.5", NOW, NOW, snap_a, None)
    lines = store.log_lines(NOW - timedelta(minutes=1), NOW)
    assert lines and lines[0][1] == "sw-a" and lines[0][0] == NOW
    assert store.last_poll("sw-a") == (NOW, True, None) and store.last_poll("nope") is None
    ports = store.mac_ports("sw-a", NOW - timedelta(minutes=1), NOW)
    assert all(isinstance(v, set) for v in ports.values()) and len(ports) == len({m.mac for m in snap_a.macs})

# tests/detectors/test_mac.py addition
def test_flap_events_carry_the_other_port():
    prev = make_snapshot(macs=(MacEntry(10, "aaaa.bbbb.cccc", "Gi1/0/7"),))
    cur = make_snapshot(macs=(MacEntry(10, "aaaa.bbbb.cccc", "Gi1/0/14"),),
                        log_lines=(make_log("SW_MATM-4-MACFLAP_NOTIF", "Host aaaa.bbbb.cccc in vlan 10 is flapping between port Gi1/0/7 and port Gi1/0/14"),))
    events = mac.detect(DetectorContext(prev, cur, {}, [], frozenset()))
    assert {(e.port, e.other_port) for e in events} == {("Gi1/0/14", "Gi1/0/7"), ("Gi1/0/7", "Gi1/0/14")}
```

- [ ] **Step 2: Run them** — Expected: FAIL on imports (`Host`, `other_port`).

- [ ] **Step 3: Implement model, store and detector changes**

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/model.py src/loopwatch/store.py src/loopwatch/detectors/mac.py tests
git commit -m "feat(kit): hosts table, event other_port, log and poll queries for diagnosis and web"
```

---

### Task 6: MikroTik RouterOS parsers and profile

**Files:**
- Create: `src/loopwatch/parsers/mikrotik.py`, `tests/test_parse_mikrotik.py`, `tests/fixtures/routeros7/ccr/` with `_interface_print_without-paging.txt`, `_interface_ethernet_print_stats_without-paging.txt`, `_ip_neighbor_print_detail_without-paging.txt`, `_log_print_without-paging.txt`, `_ip_dhcp-server_lease_print_detail_without-paging.txt`, `_system_resource_print.txt`
- Modify: `src/loopwatch/profiles.py` (append `MIKROTIK_ROUTEROS`), `src/loopwatch/detectors/links.py` (RouterOS link line regex), `tests/test_ports.py`

**Interfaces:**
- Produces:
  ```python
  MIKROTIK_COMMANDS = ("/interface print without-paging", "/interface ethernet print stats without-paging",
                       "/ip neighbor print detail without-paging", "/log print without-paging",
                       "/ip dhcp-server lease print detail without-paging")
  def parse_interface_list(text) -> dict[str, str]                 # name -> link for type "ether"
  def parse_ethernet_stats(text) -> dict[str, dict[str, int]]      # name -> {key: value}
  def parse_ip_neighbors(text) -> tuple[Neighbor, ...]
  def parse_log(text, now: datetime) -> tuple[LogLine, ...]        # relevant topics only; time read as written, tagged UTC (as the Cisco parser does)
  def parse_dhcp_leases(text) -> tuple[Host, ...]
  def parse_mikrotik_snapshot(switch: str, raw: Mapping[str, str], taken_at: datetime) -> Snapshot
  MIKROTIK_ROUTEROS = Profile("mikrotik_routeros", {"mikrotik_routeros"}, MIKROTIK_COMMANDS, parse_mikrotik_snapshot,
                              "/system resource print", r"version:\s*(\S+)")
  ```
- `command_slug` turns `/interface print without-paging` into `_interface_print_without-paging` (replace `/` with `_` as well as spaces). Adjust `command_slug` and the one existing test if it asserts the old behaviour for Cisco names (unchanged: no slashes).

Fixture content (keys the tests assert; anonymised):

```
# _interface_print_without-paging.txt
Flags: D - DYNAMIC; X - DISABLED, R - RUNNING; S - SLAVE
Columns: NAME, TYPE, ACTUAL-MTU, L2MTU, MAX-L2MTU, MAC-ADDRESS
#    NAME          TYPE    ACTUAL-MTU  L2MTU  MAX-L2MTU  MAC-ADDRESS
0  R ether1        ether         1500   1592      10218  DC:2C:6E:00:00:01
1    ether2        ether         1500   1592      10218  DC:2C:6E:00:00:02
2  X ether3        ether         1500   1592      10218  DC:2C:6E:00:00:03
3 RS ether5        ether         1500   1592      10218  DC:2C:6E:00:00:05
4  R sfp-sfpplus1  ether         1500   1592      10218  DC:2C:6E:00:00:0D
5  R bridge1       bridge        1500   1592             DC:2C:6E:00:00:01
```

```
# _interface_ethernet_print_stats_without-paging.txt  (two blocks: ether1 ether2 ether3 | ether5 sfp-sfpplus1)
                     name:  ether1   ether2   ether3
                 rx-bytes:  9000000  8000000  0
                rx-packet:  100000   200000   0
             rx-broadcast:  1000     2000     0
             rx-multicast:  500      600      0
             rx-fcs-error:  0        0        0
                 tx-bytes:  1000000  2000000  0
                tx-packet:  110000   210000   0
             tx-collision:  0        0        0

                     name:  ether5   sfp-sfpplus1
                 rx-bytes:  7000000  6000000
                rx-packet:  300000   400000
             rx-broadcast:  12000    100
             rx-multicast:  700      50
             rx-fcs-error:  3        0
                 tx-bytes:  3000000  4000000
                tx-packet:  310000   410000
             tx-collision:  0        0
```

```
# _ip_neighbor_print_detail_without-paging.txt
 0 interface=ether5 address=10.0.0.12 address4=10.0.0.12 mac-address=00:11:22:33:44:55 
   identity="sw-floor2-a" platform="cisco" version="Cisco IOS Software, C2960X Software" unpack=none 
   age=10s uptime=3d2h interface-name="GigabitEthernet1/0/48" system-caps=bridge,router 
   system-caps-enabled=bridge discovered-by=cdp,lldp

 1 interface=ether2 address=10.0.0.40 mac-address=AA:BB:CC:00:00:40 identity="office-ap-1" 
   platform="Ubiquiti" system-caps=bridge,wlan-ap system-caps-enabled=bridge,wlan-ap discovered-by=lldp
```

```
# _log_print_without-paging.txt
 nov/08/2025 09:00:00 system,info,account user admin logged in from 10.0.0.9 via ssh
 nov/08 23:59:58 dhcp,info dhcp-lan assigned 10.0.10.183 for A4:B1:C2:D3:E4:F5 tp-link-switch
 10:42:11 interface,info ether5 link down
 10:42:12 interface,info ether5 link up (speed 1G, full duplex)
 10:42:30 bridge,stp bridge1: topology change
 10:43:00 system,error,critical login failure for user x from 10.0.0.99 via ssh
 10:43:05 firewall,info forward: in:bridge1 out:ether1, src-mac aa:bb:cc:dd:ee:ff, proto TCP
```

```
# _ip_dhcp-server_lease_print_detail_without-paging.txt
 0 D address=10.0.10.183 mac-address=A4:B1:C2:D3:E4:F5 client-id=1:a4:b1:c2:d3:e4:f5 address-lists="" 
   server=dhcp-lan dhcp-option="" status=bound expires-after=9h58m last-seen=1m12s 
   active-address=10.0.10.183 active-mac-address=A4:B1:C2:D3:E4:F5 active-server=dhcp-lan host-name="tp-link-switch"

 1   address=10.0.10.57 mac-address=3C:22:FB:10:9A:01 server=dhcp-lan status=waiting last-seen=2d host-name="mbp-jdv"
```

```
# _system_resource_print.txt
                   uptime: 3d2h14m
                  version: 7.16.1 (stable)
               build-time: 2024-10-10 14:00:00
              board-name: CCR2116-12G-4S+
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_parse_mikrotik.py
from loopwatch.parsers.mikrotik import (parse_dhcp_leases, parse_ethernet_stats, parse_interface_list,
    parse_ip_neighbors, parse_log, parse_mikrotik_snapshot, MIKROTIK_COMMANDS)
from loopwatch.profiles import MIKROTIK_ROUTEROS, profile_for, version_of
from loopwatch.transport import command_slug
from tests.conftest import FIXTURES, NOW
ROOT = FIXTURES / "routeros7" / "ccr"
def raw(): return {c: (ROOT / f"{command_slug(c)}.txt").read_text() for c in MIKROTIK_COMMANDS}

def test_interface_list_link_states():
    links = parse_interface_list(raw()[MIKROTIK_COMMANDS[0]])
    assert links == {"ether1": "connected", "ether2": "notconnect", "ether3": "disabled", "ether5": "connected", "sfp-sfpplus1": "connected"}

def test_ethernet_stats_join_across_blocks():
    stats = parse_ethernet_stats(raw()[MIKROTIK_COMMANDS[1]])
    assert stats["ether5"]["rx-broadcast"] == 12000 and stats["sfp-sfpplus1"]["rx-packet"] == 400000
    assert stats["ether3"]["rx-packet"] == 0 and len(stats) == 5

def test_ip_neighbors_capabilities_mapped():
    n = {x.port: x for x in parse_ip_neighbors(raw()[MIKROTIK_COMMANDS[2]])}
    assert n["ether5"].name == "sw-floor2-a" and n["ether5"].remote_port == "Gi1/0/48" and n["ether5"].is_bridge
    assert n["ether2"].capabilities == frozenset({"B", "W"}) and "Ubiquiti" in n["ether2"].platform

def test_log_keeps_relevant_topics_and_maps_link_lines():
    lines = parse_log(raw()[MIKROTIK_COMMANDS[3]], NOW)
    texts = [l.text for l in lines]
    assert "ether5 link down" in texts and "bridge1: topology change" in texts
    assert any("login failure" in t for t in texts) and "tp-link-switch" in " ".join(texts)
    assert not any("firewall" in (l.mnemonic or "") for l in lines) and not any("logged in" in t for t in texts)
    down = next(l for l in lines if l.text == "ether5 link down")
    assert down.mnemonic == "LINK-3-UPDOWN" and down.time == NOW.replace(hour=10, minute=42, second=11)
    assert next(l for l in lines if "tp-link-switch" in l.text).time == datetime(2026, 11, 8, 23, 59, 58, tzinfo=timezone.utc)
    assert next(l for l in lines if "topology" in l.text).mnemonic == "bridge,stp"

def test_dhcp_leases():
    hosts = {h.mac: h for h in parse_dhcp_leases(raw()[MIKROTIK_COMMANDS[4]])}
    assert hosts["a4b1.c2d3.e4f5"] == Host("a4b1.c2d3.e4f5", "10.0.10.183", "tp-link-switch", True)
    assert hosts["3c22.fb10.9a01"].active is False

def test_snapshot_shape_and_profile():
    snap = parse_mikrotik_snapshot("ccr", raw(), NOW)
    by = {i.port: i for i in snap.interfaces}
    assert by["ether5"].link == "connected" and by["ether5"].in_bcast == 12000 and by["ether5"].in_errors == 3
    assert snap.stp_vlans == () and snap.macs == () and snap.stp_summary is None and len(snap.hosts) == 2
    assert snap.parse_errors == () and len(snap.neighbors) == 2
    assert profile_for("mikrotik_routeros") is MIKROTIK_ROUTEROS
    assert version_of(MIKROTIK_ROUTEROS, (ROOT / "_system_resource_print.txt").read_text()) == "7.16.1"

def test_empty_lease_table_is_not_a_parse_error():
    r = raw(); r[MIKROTIK_COMMANDS[4]] = ""
    assert parse_mikrotik_snapshot("ccr", r, NOW).parse_errors == ()

def test_router_storm_and_link_flap_fire_but_stp_and_mac_stay_silent():
    from loopwatch.detectors.runner import run_detectors
    from loopwatch.detectors.context import DetectorContext
    from dataclasses import replace
    t0 = parse_mikrotik_snapshot("ccr", raw(), NOW)
    r = raw(); r[MIKROTIK_COMMANDS[1]] = r[MIKROTIK_COMMANDS[1]].replace("rx-broadcast:  12000", "rx-broadcast:  2012000")
    t1 = replace(parse_mikrotik_snapshot("ccr", r, NOW + timedelta(seconds=60)), taken_at=NOW + timedelta(seconds=60))
    events = run_detectors(DetectorContext(t0, t1, {"ether5": 100.0}, [], frozenset()))
    assert {e.kind for e in events} == {"broadcast_storm"} and events[0].port == "ether5"

# tests/test_ports.py addition
def test_routeros_names_are_left_alone():
    assert normalize_port("ether5") == "ether5" and normalize_port("sfp-sfpplus1") == "sfp-sfpplus1"
```

- [ ] **Step 2: Run them** — Expected: FAIL, `ModuleNotFoundError: loopwatch.parsers.mikrotik`.

- [ ] **Step 3: Write the fixtures, then implement `mikrotik.py`**

Parsing rules: `print` tables: regex `^\s*(\d+)\s+(?:([DXRS]{1,4})\s+)?(\S+)\s+(\S+)` on lines after the `#` header; `link` from flags. Stats: split on blank lines; in each block the `name:` row gives the column names, each later `key:` row splits on whitespace into as many ints as names; keep ints only. Detail lists (`neighbor`, `lease`): entries start at `^\s*\d+\s`, continuation lines are indented; tokenise with `(\S+?)=("([^"]*)"|\S*)`. Caps map: `bridge→B, switch→S, router→R, telephone→T, wlan-ap→W, station→H`, others as given. Log: `^\s*(?:(?P<mon>[a-z]{3})/(?P<day>\d\d)(?:/(?P<year>\d{4}))? )?(?P<hms>\d\d:\d\d:\d\d) (?P<topics>[a-z][a-z,-]*) (?P<msg>.*)$`; keep when topics ∩ {interface, bridge, stp, dhcp, error, critical}; mnemonic = topics, except `LINK-3-UPDOWN` for `^\S+ link (up|down)`; `text = msg`; `time` = the stamp read as written and tagged UTC, with `now`'s date when only `HH:MM:SS` is given and `now`'s year when the year is missing (a December line read in January moves back a year, as in `parsers/logging.py`). `log_reason` then shows the stamp in event reasons and the Log view, exactly as for Cisco. In `_section` for the lease command, treat empty output as `()` without a `ParseError`. In `links.py` add `_LINK_IFACE_ROS = re.compile(r"^(\S+) link (?:up|down)")` as a fallback.

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/parsers/mikrotik.py src/loopwatch/profiles.py src/loopwatch/parsers/snapshot.py src/loopwatch/detectors/links.py src/loopwatch/transport.py tests
git commit -m "feat(kit): MikroTik RouterOS profile with interface, neighbour, log and DHCP lease parsers"
```

---

### Task 7: Collector: hosts, reload, inventory diffing

**Files:**
- Modify: `src/loopwatch/collector.py`, `src/loopwatch/cli.py` (`watch`, `snapshot` pass a loader), `tests/test_collector.py`

**Interfaces:**
- Produces:
  ```python
  async def run_collector(load: Callable[[], Inventory], transport, store, *, stop: asyncio.Event,
                          reload: asyncio.Event | None = None, inventory_path: Path | None = None,
                          check_interval: float = 30.0, clock=utcnow, sleep=asyncio.sleep,
                          max_sessions=8, escalation=None, on_inventory_error: Callable[[str | None], None] | None = None) -> None
  ```
  `poll_once` unchanged signature; after parsing it calls `store.upsert_hosts(snapshot.hosts, f"dhcp:{switch.name}", now)` when `snapshot.hosts`.
- Behaviour: supervisor task loads at start, then on `reload` or every `check_interval` seconds when `inventory_path` mtime changed. Diff by `Switch` equality: start new, cancel removed, cancel+start changed. Load failure → `log.error`, `on_inventory_error(message)`, keep running set; success → `on_inventory_error(None)`. Worker exceptions other than `CancelledError` still end the run.

- [ ] **Step 1: Write the failing tests**

```python
async def test_reload_adds_removes_and_restarts_devices(store, fake, creds):
    inv = {"v": Inventory((Switch(name="sw-a", host="10.0.0.5"),), {"ssh": creds})}
    stop, reload = asyncio.Event(), asyncio.Event()
    polled: list[str] = []
    async def fake_sleep(s):
        polled.append(len(fake.calls)); await asyncio.sleep(0)
        if len(fake.calls) == 2:   # sw-a polled twice: add sw-core-1, change sw-a's interval
            inv["v"] = Inventory((Switch(name="sw-a", host="10.0.0.5", poll_interval=30), Switch(name="sw-core-1", host="10.0.0.1")), {"ssh": creds}); reload.set()
        if len(fake.calls) >= 6: stop.set()
    await run_collector(lambda: inv["v"], fake, store, stop=stop, reload=reload, clock=lambda: NOW, sleep=fake_sleep, check_interval=1000)
    names = [c[0] for c in fake.calls]
    assert names[:2] == ["sw-a", "sw-a"] and "sw-core-1" in names[2:]

async def test_reload_failure_keeps_running_set(store, fake, inv):
    stop, reload = asyncio.Event(), asyncio.Event()
    errors: list[str | None] = []
    state = {"fail": False}
    def load():
        if state["fail"]: raise InventoryError("bad yaml")
        return inv
    async def fake_sleep(s):
        await asyncio.sleep(0)
        if len(fake.calls) == 2 and not state["fail"]: state["fail"] = True; reload.set()
        if len(fake.calls) >= 4: stop.set()
    await run_collector(load, fake, store, stop=stop, reload=reload, clock=lambda: NOW, sleep=fake_sleep, check_interval=1000, on_inventory_error=errors.append)
    assert errors[0] is None and "bad yaml" in (errors[-1] or "") and {c[0] for c in fake.calls} == {"sw-a", "sw-core-1"}

def test_poll_once_upserts_dhcp_hosts(store, creds):
    fake = FakeTransport(FIXTURES / "routeros7")
    ccr = Switch(name="ccr", host="10.0.0.254", device_type="mikrotik_routeros", credentials="mikrotik")
    inv = Inventory((ccr,), {"mikrotik": creds})
    poll_once(ccr, inv, fake, store, None, NOW)
    assert store.hosts_for({"a4b1.c2d3.e4f5"})["a4b1.c2d3.e4f5"].hostname == "tp-link-switch"
```

Existing `run_collector(inv, ...)` calls in tests become `run_collector(lambda: inv, ...)`.

- [ ] **Step 2: Run them** — Expected: FAIL, `TypeError` on the new signature / `AttributeError` on hosts.

- [ ] **Step 3: Implement** (CLI `watch`: `load = lambda: _inventory(path)`; `snapshot` keeps loading once)

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/collector.py src/loopwatch/cli.py tests/test_collector.py
git commit -m "feat(collector): reload the inventory on change and store DHCP hosts"
```

---

### Task 8: Ranking labels, time zone, log section

**Files:**
- Modify: `src/loopwatch/report/ranking.py`, `src/loopwatch/report/render.py`, `src/loopwatch/report/templates/report.html.j2`, `src/loopwatch/cli.py`, `tests/report/test_ranking.py`, `tests/report/test_render.py`
- Create: `src/loopwatch/report/templates/report_body.html.j2` (the sections; `report.html.j2` wraps it in a standalone page; Task 10 includes it in the web layout)

**Interfaces:**
- Produces:
  ```python
  class Suspect: label: str | None = None            # "uplink" | "ap" | None
  def rank_suspects(events, uplinks: Set[tuple[str,str]], ap_ports: Set[tuple[str,str]] = frozenset()) -> list[Suspect]
  def ap_ports_from_neighbors(neighbors_by_switch) -> set[tuple[str, str]]   # capability "W"
  class ReportData: log_lines: list[tuple[datetime, str, LogLine]]; ap_ports: frozenset[tuple[str,str]]; uplinks: frozenset[tuple[str,str]]
  def build_report(store, managed, since, until, ap_ports: Set[tuple[str,str]] = frozenset()) -> ReportData
  def render_text(data, tz: tzinfo = timezone.utc) -> str
  def render_html(data, tz: tzinfo = timezone.utc) -> str
  def resolve_tz(name: str | None) -> tzinfo        # zoneinfo; None/"" -> UTC; unknown -> click-free ValueError("unknown time zone 'X'")
  ```
- CLI: `LOOPWATCH_TZ` read in `main` into `ctx.obj["tz"]`; `report` and `snapshot` pass it; `build_report` gets `inv.ap_ports()` when the inventory loads.

- [ ] **Step 1: Write the failing tests**

```python
# test_ranking.py
def test_ap_port_scores_half_and_is_labelled():
    events = [make_event("mac_flap", "Gi1/0/20", 1, mac="aaaa.bbbb.cccc"), make_event("mac_flap", "Gi1/0/14", 1, mac="aaaa.bbbb.cccc")]
    s = {x.port: x for x in rank_suspects(events, set(), {("sw-a", "Gi1/0/20")})}
    assert s["Gi1/0/20"].score == 10 and s["Gi1/0/20"].label == "ap" and s["Gi1/0/14"].score == 20 and s["Gi1/0/14"].label is None

def test_ap_ports_from_lldp_capability():
    n = {"sw-a": [Neighbor("Gi1/0/20", "office-ap-1", "eth0", "Ubiquiti", frozenset({"B", "W"})), Neighbor("Gi1/0/48", "sw-core-1", "Te1/1/2", "cisco", frozenset({"S"}))]}
    assert ap_ports_from_neighbors(n) == {("sw-a", "Gi1/0/20")}

# test_render.py
def test_times_render_in_requested_zone(report_data):   # existing fixture with an event at NOW (10:00 UTC)
    from zoneinfo import ZoneInfo
    assert "11:00:00" in render_text(report_data, ZoneInfo("Europe/Amsterdam"))
    assert "10:00:00" in render_text(report_data)

def test_html_has_log_section_and_labels(store_with_log):   # record snap_a then build_report
    data = build_report(store_with_log, {"sw-a"}, NOW - timedelta(hours=1), NOW)
    html = render_html(data)
    assert "<h2>Log</h2>" in html and "MACFLAP" in html

def test_resolve_tz():
    assert resolve_tz(None).utcoffset(NOW) == timedelta(0)
    with pytest.raises(ValueError, match="unknown time zone 'Mars/Olympus'"):
        resolve_tz("Mars/Olympus")
```

- [ ] **Step 2: Run them** — Expected: FAIL on signatures / missing names.

- [ ] **Step 3: Implement**; text report shows `(uplink)`/`(ap)` after the score; Log section columns: polled time, device, device stamp (or `-`), line.

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/report src/loopwatch/cli.py tests/report
git commit -m "feat(report): access-point ports, log section and local time rendering"
```

---

### Task 9: Diagnosis

**Files:**
- Create: `src/loopwatch/report/diagnosis.py`, `tests/report/test_diagnosis.py`
- Modify: `src/loopwatch/report/render.py` (Diagnosis section first in text and HTML), `report_body.html.j2`, `pyproject.toml` (add `netaddr`)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class HostInfo: mac: str; vendor: str; hostname: str | None; ip: str | None; other_port: str | None
  @dataclass(frozen=True)
  class Diagnosis:
      switch: str; port: str; first_event: datetime
      pattern: str            # "bridge_on_edge" | "wireless_bridge" | "cable_between_ports" | "unknown"
      headline: str
      connected: Neighbor | None
      attached: tuple[HostInfo, ...]
      flapping: tuple[HostInfo, ...]
      notes: tuple[str, ...]
  def vendor_of(mac: str) -> str                      # netaddr OUI org name or "unknown vendor"
  def diagnose(store, data: ReportData, vendor: Callable[[str], str] = vendor_of) -> Diagnosis | None
  class ReportData: diagnosis: Diagnosis | None       # set by build_report after ranking
  ```
- Rules: top suspect → `(switch, port)`; `first_event` = earliest event on that port in the window. `connected` = neighbour on the port in `store.latest_neighbors(switch, until)`. `attached` = MACs from `store.mac_ports(switch, first_event - 1h, first_event)` seen only on `port`. `flapping` = distinct `(mac, other_port)` from `mac_flap` events on the port. Pattern: any `unexpected_neighbor` on the port → `bridge_on_edge`; else flapping and every `other_port` is an AP port of that switch → `wireless_bridge`; else flapping with exactly one distinct `other_port`, no `connected`, no `attached` → `cable_between_ports`; else `unknown`. Headlines (exact): `"The loop most likely entered on {switch} {port}"`; append `": a bridge appeared on this edge port"` / `": wireless-to-wired bridge on this port"` / `": cable or unmanaged device between {port} and {other}"` / `""`. Note when a hostname is shared between an attached and a flapping MAC: `"{hostname} is on both sides: its Wi-Fi and wired adapters are bridged"`. Notes when `attached` and `connected` are both empty: `"No neighbour or MAC was learned on this port before the loop; the device is unknown."`; when `hosts_for` found nothing: `"No DHCP leases known; hostnames and IPs are unavailable."`

- [ ] **Step 1: Write the failing tests** (use `tests/detectors/builders.py`, an in-memory `Store`, and a stub vendor `lambda m: "V-" + m[:4]`)

```python
def test_bridge_on_edge_with_lldp_neighbor(store_bridge_scenario):   # fixture: snap with Neighbor on Gi1/0/14 caps {"B"}, events unexpected_neighbor + 2 mac_flap(other_port Te1/1/2) on Gi1/0/14, hosts for both macs
    d = build_report(store, {"sw-a", "sw-core-1"}, NOW - timedelta(hours=1), NOW).diagnosis
    assert d.pattern == "bridge_on_edge" and d.connected.name == "TL-SG108" and d.headline.endswith("a bridge appeared on this edge port")
    assert {h.other_port for h in d.flapping} == {"Te1/1/2"} and d.flapping[0].hostname == "fileserver01"

def test_wireless_bridge_pattern_and_same_host_note(store_wireless_scenario):  # mac table 1h before: laptop wired mac only on Gi1/0/14; flaps other_port Gi1/0/20; ap_ports {("sw-a","Gi1/0/20")}; hosts: both macs hostname "mbp-jdv"
    d = build_report(store, {"sw-a"}, since, until, ap_ports={("sw-a", "Gi1/0/20")}).diagnosis
    assert d.pattern == "wireless_bridge" and d.port == "Gi1/0/14"
    assert any("mbp-jdv is on both sides" in n for n in d.notes) and d.attached[0].vendor.startswith("V-")

def test_cable_between_ports(store_cable_scenario):   # flaps Gi1/0/3 <-> Gi1/0/9 only, no neighbours, no earlier macs on the port
    d = build_report(...).diagnosis
    assert d.pattern == "cable_between_ports" and "between Gi1/0/3 and Gi1/0/9" in d.headline

def test_no_suspects_gives_none_and_router_top_suspect_has_empty_lists(store):
    assert build_report(store, set(), NOW - timedelta(hours=1), NOW).diagnosis is None
    store.write_events([Event(NOW, "ccr", "broadcast_storm", "high", "storm", port="ether5")])
    d = build_report(store, {"ccr"}, NOW - timedelta(hours=1), NOW).diagnosis
    assert d.pattern == "unknown" and d.attached == () and d.flapping == () and "device is unknown" in " ".join(d.notes)

def test_vendor_of_known_and_unknown():
    assert "VMware" in vendor_of("0050.56a1.0b3c") and vendor_of("0200.0000.0001") == "unknown vendor"

def test_diagnosis_renders_first_in_text_and_html(store_bridge_scenario):
    data = build_report(...)
    text = render_text(data); assert text.index("Diagnosis") < text.index("Suspects")
    html = render_html(data); assert "<h2>Diagnosis</h2>" in html and "TL-SG108" in html
```

- [ ] **Step 2: Run them** — Expected: FAIL, `ModuleNotFoundError: loopwatch.report.diagnosis`.

- [ ] **Step 3: Add `netaddr` to dependencies, `.venv/bin/pip install -e .[dev]`, implement**

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/loopwatch/report tests/report
git commit -m "feat(report): diagnose the entry port, connected device and MACs with hostname and vendor"
```

---

### Task 10: Web UI, read side

**Files:**
- Create: `src/loopwatch/web/__init__.py`, `src/loopwatch/web/app.py`, `src/loopwatch/web/templates/base.html.j2`, `home.html.j2`, `report.html.j2`, `src/loopwatch/web/static/style.css`, `tests/web/__init__.py`, `tests/web/conftest.py`, `tests/web/test_pages.py`
- Modify: `pyproject.toml` (deps `aiohttp`; dev `pytest-aiohttp`; hatch include `web/templates/*.j2`, `web/static/*`)

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class WebContext:
      store: Store
      inventory_path: Path
      env: Mapping[str, str]
      tz: tzinfo
      transport: Transport
      reload: asyncio.Event
      collector_alive: Callable[[], bool]
      inventory_error: Callable[[], str | None]
  def create_app(ctx: WebContext) -> aiohttp.web.Application
  async def serve(app, port: int) -> aiohttp.web.AppRunner      # binds 0.0.0.0; OSError propagates
  ```
- Routes this task: `GET /`, `GET /report`, `GET /healthz`, `GET /static/style.css`. Jinja via `aiohttp_jinja2`? No: plain `jinja2.Environment(PackageLoader("loopwatch.web", "templates"), autoescape=True)` and `web.Response(text=..., content_type="text/html")`. `report.html.j2` includes `loopwatch.report`'s `report_body.html.j2` through a `ChoiceLoader` of both packages.
- Store calls run in `asyncio.to_thread`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/web/conftest.py: fixtures `ctx` (in-memory Store seeded with snap_a + events, tmp inventory with sw-a and sw-core-1, env with LOOPWATCH_SSH_USER/PASSWORD="hunter2", tz UTC, FakeTransport(ios15), reload Event, collector_alive lambda: True) and `client` = aiohttp_client(create_app(ctx))

async def test_home_lists_devices_and_suspects(client):
    r = await client.get("/"); body = await r.text()
    assert r.status == 200 and "sw-a" in body and "Gi1/0/14" in body and "hunter2" not in body

async def test_home_with_empty_inventory_says_so(client_empty):
    body = await (await client_empty.get("/")).text()
    assert "No devices yet" in body and "Add device" in body

async def test_home_shows_inventory_error(client_with_error):   # inventory_error returns "bad yaml"
    assert "bad yaml" in await (await client_with_error.get("/")).text()

async def test_report_since_and_window(client):
    assert (await client.get("/report?since=2h")).status == 200
    r = await client.get("/report?from=2026-11-09T09:00&to=2026-11-09T11:00"); body = await r.text()
    assert r.status == 200 and "<h2>Timeline</h2>" in body
    assert (await client.get("/report?since=bogus")).status == 400

async def test_healthz(client, ctx):
    r = await client.get("/healthz"); assert r.status == 200 and (await r.json())["devices"] == 2
    ctx.collector_alive = lambda: False
    assert (await client.get("/healthz")).status == 503
```

- [ ] **Step 2: Run them** — Expected: FAIL, `ModuleNotFoundError: loopwatch.web`.

- [ ] **Step 3: Add dependencies, install, implement app, templates (layout from the approved mockups: header with Home/Report/Devices, cards, badges, light/dark via `prefers-color-scheme`, phone width) and CSS**

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/loopwatch/web tests/web
git commit -m "feat(web): home, report and health pages served by aiohttp"
```

---

### Task 11: Web UI, device editing

**Files:**
- Create: `src/loopwatch/web/templates/devices.html.j2`, `device_form.html.j2`, `tests/web/test_devices.py`
- Modify: `src/loopwatch/web/app.py`

**Interfaces:**
- Routes: `GET /devices`, `GET|POST /devices/new`, `GET|POST /devices/{name}`, `POST /devices/{name}/delete`, `POST /devices/{name}/test`.
- Form fields: `name, host, device_type, credentials, poll_interval, ap_ports` (comma separated), `skip` (multiple checkbox values). POST: build `Switch`, `validate_device(switch, credential_sets_from_env(env).keys())`; errors → re-render 400 with messages; ok → `save_inventory`, `reload.set()`, 303 to `/devices`. Delete: 303. Test: `transport.run(switch, creds, [profile.version_command])` in `to_thread`, render the form with the version or the `TransportError.message`.
- Middleware `same_origin`: for POST, if `Origin` header present and its host (scheme-stripped, port kept) differs from `Host` → 403 before any handler.

- [ ] **Step 1: Write the failing tests**

```python
async def test_add_edit_delete_round_trip(client, ctx):
    r = await client.post("/devices/new", data={"name": "ccr", "host": "10.0.0.254", "device_type": "mikrotik_routeros",
                          "credentials": "mikrotik", "poll_interval": "30", "ap_ports": "", "skip": []}, allow_redirects=False)
    assert r.status == 303 and ctx.reload.is_set()
    inv = load_inventory(ctx.inventory_path, ctx.env); assert inv.by_name("ccr").poll_interval == 30
    r = await client.post("/devices/ccr", data={"name": "ccr", "host": "10.0.0.253", "device_type": "mikrotik_routeros",
                          "credentials": "mikrotik", "poll_interval": "60", "ap_ports": "ether2", "skip": []}, allow_redirects=False)
    assert r.status == 303 and load_inventory(ctx.inventory_path, ctx.env).by_name("ccr").ap_ports == frozenset({"ether2"})
    assert (await client.post("/devices/ccr/delete", allow_redirects=False)).status == 303
    assert "ccr" not in load_inventory(ctx.inventory_path, ctx.env).names

async def test_validation_errors_are_shown(client):
    r = await client.post("/devices/new", data={"name": "bad name", "host": "h", "device_type": "cisco_ios", "credentials": "ssh", "poll_interval": "5", "ap_ports": "", "skip": []})
    body = await r.text(); assert r.status == 400 and "between 10 and 3600" in body and "Name must" in body

async def test_form_offers_credential_set_names_only(client):
    body = await (await client.get("/devices/new")).text()
    assert "ssh" in body and "mikrotik" in body and "hunter2" not in body

async def test_test_connection_success_and_failure(client, client_unreachable):
    body = await (await client.post("/devices/sw-a/test")).text(); assert "15.2" in body
    body = await (await client_unreachable.post("/devices/sw-a/test")).text(); assert "timed out" in body and "hunter2" not in body

async def test_cross_origin_post_is_refused(client, ctx):
    r = await client.post("/devices/sw-a/delete", headers={"Origin": "http://evil.example"}, allow_redirects=False)
    assert r.status == 403 and "sw-a" in load_inventory(ctx.inventory_path, ctx.env).names
    r = await client.post("/devices/sw-a/delete", headers={"Origin": f"http://{client.host}:{client.port}"}, allow_redirects=False)
    assert r.status == 303
```

- [ ] **Step 2: Run them** — Expected: FAIL with 404s.

- [ ] **Step 3: Implement routes, middleware and templates**

- [ ] **Step 4: Run the suite** — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/web tests/web
git commit -m "feat(web): add, edit, delete and test devices from the browser"
```

---

### Task 12: Wire `watch`, Docker, README

**Files:**
- Modify: `src/loopwatch/cli.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `inventory.example.yaml`, `README.md`, `tests/test_cli.py`

**Interfaces:**
- `watch --no-web` flag; `LOOPWATCH_WEB_PORT` (default 8080) via `envvar` on a `--port` option; `watch` creates `reload = asyncio.Event()`, a `collector_alive` closure over the collector task, an `inventory_error` holder fed by `on_inventory_error`, builds `WebContext`, runs `serve()` then `run_collector()`; bind failure → `click.ClickException(f"cannot listen on port {port}: {exc}")`.
- Dockerfile: `ENV LOOPWATCH_INVENTORY=/data/inventory.yaml LOOPWATCH_DB=/data/loopwatch.sqlite LOOPWATCH_WEB_PORT=8080`, `EXPOSE 8080`, `HEALTHCHECK` as in the compose file.
- compose: as agreed in chat (Portainer version with `network_mode: host`, `${LOOPWATCH_*}` references, healthcheck, log rotation) plus a commented `env_file: .env` line for plain compose hosts; `image: ghcr.io/patrickb100/loop-watch:latest` with a commented `build: .`.
- `.env.example`: as agreed in chat (ssh and mikrotik sets, paths, web port, tz).
- README: Portainer section (stack text, environment variables, package visibility: public, or `ghcr.io` registry credential with `read:packages`), web UI section, MikroTik section (`read` group user), Cisco preparation section (the config blocks from chat: account and SSH, logging buffer and timestamps, `mac address-table notification mac-move`, `lldp run`, optional BPDU guard and storm control, hostname = inventory name), recording RouterOS fixtures with `snapshot ccr --record`.

- [ ] **Step 1: Write the failing test**

```python
def test_watch_no_web_flag_and_port_option_exist():
    from click.testing import CliRunner
    out = CliRunner().invoke(main, ["watch", "--help"]).output
    assert "--no-web" in out and "--port" in out and "LOOPWATCH_WEB_PORT" in out
```

- [ ] **Step 2: Run it** — Expected: FAIL.

- [ ] **Step 3: Implement CLI wiring; update Dockerfile, compose, env example, inventory example, README**

- [ ] **Step 4: Run the suite, build the image, smoke-run the page**

Run: `.venv/bin/pytest -q`
Expected: all pass.
Run (Docker available): `docker build -q -t loop-watch:plan . && docker run --rm -d --name lw-smoke -p 18080:8080 -e LOOPWATCH_SSH_USER=x -e LOOPWATCH_SSH_PASSWORD=y -v lw-smoke:/data loop-watch:plan && sleep 3 && curl -s localhost:18080/healthz; docker rm -f lw-smoke; docker volume rm lw-smoke`
Expected: `{"status": "ok", "devices": 0, ...}`.
Also run the suite inside the image: `docker run --rm -v "$PWD/tests:/app/tests:ro" loop-watch:plan sh -c "pip install -q .[dev] && pytest -q"` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/loopwatch/cli.py Dockerfile docker-compose.yml .env.example inventory.example.yaml README.md tests/test_cli.py
git commit -m "feat(web): serve the UI from watch; inventory on the data volume; Portainer deployment docs"
```

---

### Task 13: Release 0.2.0

- [ ] **Step 1: Bump `version = "0.2.0"` in `pyproject.toml`**
- [ ] **Step 2: Run the suite** — Expected: all pass.
- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore(release): version 0.2.0"
```
