# loop-watch v2 design: web UI, device profiles, MikroTik

Date: 2026-11-09
Status: draft for review
Builds on: [2026-11-09-loop-watch-design.md](2026-11-09-loop-watch-design.md) (v1)

## Problem

v1 is a command-line service. Reading a report means a shell on the Docker
host, and adding a switch means editing a YAML file that is mounted
read-only. It only speaks to Cisco Catalyst switches, while the office edge
is a MikroTik CCR2116 whose log and interface counters see the same storm.
Deployment through Portainer needs a published image, a writable inventory
and a port for the page.

## Goal

- A web page on the Docker host that shows the current state, the report for
  any time window, and lets you add, edit, remove and test devices.
- The MikroTik router as a monitored device, contributing its log, interface
  rates and neighbours to the same timeline and ranking.
- A published container image on GitHub Container Registry that a Portainer
  stack can pull, with the inventory living on the data volume.
- Times shown in the office time zone.
- A diagnosis at the top of every report: the port the loop most likely
  entered on, the device connected there, and the MAC addresses involved
  with hostname, IP address and vendor where known.

Decisions already taken with the owner: device list editable from the page,
credentials stay in the environment; no login, LAN only, `network_mode:
host`; MikroTik collects log, interface rates and neighbours, not STP or the
MAC table.

## Out of scope

- Detector thresholds in the UI.
- Login or any access control on the page.
- Alerting, syslog receiver (still v3 candidates).
- MikroTik bridge host (MAC) table and STP state.
- Hostnames from DNS or ARP; only DHCP leases and LLDP/CDP are used.
- Running the container as a non-root user (would break upgrades of an
  existing root-owned volume; deferred).

## Architecture

```
                +------------------------------------------------+
                |  loop-watch container, one process             |
 switches  <-SSH|  collector  <--reload--  web (aiohttp, :8080)  |
 router    <-SSH|     |                        |                 |
                |  profiles -> parsers -> detectors               |
                |     |                        |                 |
                |  store (SQLite)  <------------+                 |
                |  inventory.yaml  <--- write --+   (on /data)   |
                +------------------------------------------------+
```

`watch` starts the collector and the web server in the same asyncio loop.
They share one `Store` and one inventory file. The web server calls the
store and the transport through `asyncio.to_thread`, as the collector does.

### New and changed modules

| Module | Change |
|---|---|
| `profiles` | New. One `Profile` per device family: commands, parse function, version command and regex. Replaces the hard-wired Cisco command list in `inventory` and the hard-wired parse in `parsers/snapshot`. |
| `parsers/mikrotik.py` | New. Hand-written parsers for the four RouterOS commands. |
| `inventory` | `Switch` gains `credentials` (set name). `Inventory` holds all credential sets. New `save_inventory`, `validate_device`. |
| `transport` | `NetmikoTransport` stops sending `terminal length 0`; netmiko disables paging itself on both platforms and RouterOS rejects the command. |
| `collector` | `run_collector` takes a loader and a `reload` event instead of a fixed inventory; starts, stops and restarts per-device workers on change. |
| `store` | `log_lines(since, until)` for the log page; `last_poll(switch)` for the home page; `hosts` table and `other_port` column; `macs_on_port(switch, port, since, until)`. |
| `report/diagnosis.py` | New. Builds the Diagnosis for the top suspect (see Culprit identification). |
| `report/render` | `tz` parameter; Diagnosis section; `fmt_time` renders in that zone. HTML report body becomes a partial that the web layout includes. |
| `web` | New package: aiohttp app, routes, templates. |
| `cli` | `watch` starts the web server; `--no-web`; `check-inventory` and `snapshot` use the profile's version command. Env `LOOPWATCH_WEB_PORT`, `LOOPWATCH_TZ`. |
| `.github/workflows` | New: `ci.yml` (pytest on every push and PR), `publish.yml` (image to ghcr.io on push to `main` and on `v*` tags). |
| `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `README.md` | Inventory default moves to `/data/inventory.yaml`; compose matches the Portainer stack in the README. |

## Device profiles

```python
@dataclass(frozen=True)
class Profile:
    family: str                       # "cisco_ios" | "mikrotik_routeros"
    device_types: frozenset[str]      # netmiko device_type values that map here
    commands: tuple[str, ...]
    parse: Callable[[str, Mapping[str, str], datetime], Snapshot]
    version_command: str
    version_pattern: str              # regex with one group

PROFILES: tuple[Profile, ...]
def profile_for(device_type: str) -> Profile   # raises InventoryError when unknown
```

- `cisco_ios` family: device types `cisco_ios`, `cisco_xe`; the v1 eight
  commands; `parse_snapshot` from v1; `show version` with
  `Version ([^,\s]+)`.
- `mikrotik_routeros` family: device type `mikrotik_routeros`; commands
  below; `parse_mikrotik_snapshot`; `/system resource print` with
  `version:\s*(\S+)`.

`Switch.commands` becomes `profile_for(device_type).commands` minus `skip`.
Inventory validation rejects a `skip` entry that is not one of the profile's
commands.

## MikroTik collection

Reached over SSH with netmiko `mikrotik_routeros`. A RouterOS user in the
built-in `read` group is enough: it can `print` and read the log and cannot
change anything.

| Command | Used for |
|---|---|
| `/interface print without-paging` | Link state per interface from the flags column: `R` running → `connected`, `X` → `disabled`, neither → `notconnect`. |
| `/interface ethernet print stats without-paging` | `rx-packet`, `tx-packet`, `rx-broadcast`, `rx-multicast`; `in_errors` = sum of `rx-*` keys containing `error`, `fcs`, `align`, `overflow`, `too-short`, `too-long`; `out_errors` = sum of `tx-*` keys containing `error`, `collision`, `underrun`. Layout: property rows with one column per interface, repeated in blocks that each start with a `name:` row. |
| `/ip neighbor print detail without-paging` | `interface` → port, `identity` → name, `interface-name` → remote port, `platform` + `board` → platform, `system-caps` → capabilities. |
| `/log print without-paging` | Lines whose topics include `interface`, `bridge`, `stp`, `dhcp`, `error` or `critical`. |
| `/ip dhcp-server lease print detail without-paging` | `mac-address`, `address`, `host-name`, `status` for the hosts table. Empty output means no leases, not a parse error. |

Mapping into the shared `Snapshot`:

- `interfaces`: joined from the first two commands on interface name. Ports
  keep RouterOS names (`ether5`, `sfp-sfpplus1`); `normalize_port` leaves
  names without a known Cisco prefix unchanged.
- `neighbors`: `system-caps` tokens are mapped to the letters the model
  already understands: `bridge` → `B`, `switch` → `S`, `router` → `R`,
  `telephone` → `T`, `wlan-ap` → `W`, `station` → `H`, unknown tokens kept
  as is. `is_bridge` then works unchanged. A neighbour `identity` that is an
  inventory name makes that router port an uplink for the ranking, as with
  Cisco CDP.
- `log_lines`: RouterOS stamps are `HH:MM:SS` (today), `mon/DD HH:MM:SS`
  (this year) or `mon/DD/YYYY HH:MM:SS`. As in v1 the event time is the poll
  time and the stamp travels in the reason text. `mnemonic` is the topics
  string (for example `interface,info`), except that `link down` and `link
  up` lines get `LINK-3-UPDOWN` so the v1 link-flap log rule counts them.
- `stp_vlans`, `stp_ports`, `macs` are empty and `stp_summary` is `None`.
  The storm, link-flap, unreachable and parse-error detectors fire on the
  router; the STP, MAC-flap and edge detectors have nothing to compare and
  stay silent. This is by design: the router tells us when the storm hit
  its ports and what it logged, not which access port started it.

The output layouts above are written from RouterOS 7 documentation and
memory, not from the CCR2116. They are a hypothesis until
`loop-watch snapshot ccr --record tests/fixtures/routeros7` has been run
once against the real router; the parsers must then be corrected against
that capture. A layout the parser cannot read becomes a `parse_error` event,
never a crash, as in v1.

## Culprit identification

The report answers "which device" above the suspect table, in a Diagnosis
section built from stored data for the top suspect `(switch, port)`:

1. **Entry point**: the suspect port and the time of its first event in the
   window.
2. **Connected device**: the CDP/LLDP neighbour on that port in the latest
   snapshot at or before the window end (identity, platform, capabilities).
   Without one: the MACs learned on that port, and on no other port of that
   switch, in the hour before the first event, from `mac_entries`. These
   are the devices physically attached. An unmanaged switch that sends no
   frames of its own shows up only through its clients, and the text says
   so.
3. **Flapping MACs**: distinct MACs from `mac_flap` events naming the port,
   each with the other port of the flap. The text explains that these are
   usually victims seen through the loop (the gateway, servers), not the
   cause. When every flap pairs the same two ports and no new neighbour or
   MAC appeared on either, the diagnosis reads "cable or unmanaged device
   between A and B" and names both ports.
4. **Per MAC**: vendor from the OUI prefix (offline, `netaddr`), and
   hostname, IP address and lease status from the `hosts` table.

Supporting changes:

- `Event` gains `other_port: str | None = None`, written by the MAC-flap
  detector and read by the diagnosis. The `events` table gets the column
  with an `ALTER TABLE` on `init_schema` when it is missing, so an existing
  v1 database keeps working.
- New `hosts(mac PRIMARY KEY, ip, hostname, source, seen_at)` table,
  upserted from every MikroTik poll's lease table (`source = "dhcp:<device>"`)
  and from CDP/LLDP neighbours that carry a management address
  (`source = "lldp:<device>"`). The latest sighting wins. Pruned with the
  180-day event retention.
- `Snapshot` gains `hosts: tuple[Host, ...]` (`Host(mac, ip, hostname,
  active)`), empty for Cisco devices in this version.
- `report/diagnosis.py`: `diagnose(store, data, until) -> Diagnosis | None`,
  rendered at the top of the text and HTML reports and on the home page for
  the last two hours. Returns `None` when the window has no suspects.
- Vendor lookup through `netaddr`'s bundled IEEE OUI registry; an unknown
  prefix renders as "unknown vendor". `netaddr` is the second new
  dependency after `aiohttp`.

Limits, stated in the report text: without DHCP leases or LLDP the device
is known only by MAC and vendor; a loop made by one cable between two wall
ports has no new device at all, only the two ports.

## Credentials

Credential sets come from the environment, never from the inventory file,
the database, logs or the page.

- Every `LOOPWATCH_<NAME>_USER` with a matching `LOOPWATCH_<NAME>_PASSWORD`
  defines set `<name>` (lower-cased). `LOOPWATCH_<NAME>_ENABLE` is optional.
  `LOOPWATCH_SSH_*` from v1 therefore becomes the set `ssh`, and
  `LOOPWATCH_MIKROTIK_*` the set `mikrotik`.
- Inventory entries get `credentials: <name>`, default `ssh`, overridable in
  `defaults`. Loading fails with a clear message when an entry names a set
  the environment does not define.
- `Inventory.credentials_for(switch) -> Credentials` replaces the single
  `Inventory.credentials`. `Credentials.__repr__` keeps redacting.
- The web page shows set names only, in a select, read from the
  environment at request time.

## Inventory file

```yaml
switches:
  - name: sw-core-1
    host: 10.0.0.1
  - name: ccr
    host: 10.0.0.254
    device_type: mikrotik_routeros
    credentials: mikrotik
defaults:
  device_type: cisco_ios
  credentials: ssh
  poll_interval: 60
```

- `save_inventory(path, switches, defaults)` writes YAML with the same
  shape, to a temporary file in the same directory followed by
  `os.replace`, so a reader never sees a half-written file. The file lives
  in a directory on the data volume, where `os.replace` works (it does not
  across a single-file bind mount).
- `validate_device(switch, credential_sets) -> list[str]` returns human
  messages: name matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`; host has no
  whitespace; device type known; poll interval 10 to 3600 seconds; every
  skipped command belongs to the profile; credential set exists. The web
  form shows the messages next to the fields; the loader rejects a file that
  fails them.
- Default location `/data/inventory.yaml` in the image. A missing file is an
  empty inventory, not an error: the service starts with zero devices and
  the page says so, so a fresh Portainer stack comes up and you add devices
  in the browser.

## Collector reload

```python
async def run_collector(
    load: Callable[[], Inventory],
    transport: Transport,
    store: Store,
    *,
    stop: asyncio.Event,
    reload: asyncio.Event,
    ...
) -> None
```

- The supervisor keeps `workers: dict[str, (Switch, Task)]`. On start, on
  `reload`, and every 30 seconds when the inventory file's mtime changed,
  it calls `load()` and diffs by `Switch` equality: new name → start a
  worker; name gone → cancel; same name, different fields → cancel and
  start. Unchanged workers keep their in-memory `prev` and escalation state.
- A load that fails (bad YAML, unknown credential set) is logged at ERROR
  and the running set is kept. The page shows the error.
- Cancelling a worker whose poll is in flight lets the thread finish; its
  result is dropped. A worker's `prev` after restart comes from
  `store.latest_snapshot`, as on service start.
- The web server sets `reload` after every successful save.

## Web UI

aiohttp, served from `watch` on `0.0.0.0:${LOOPWATCH_WEB_PORT}` (default
8080). Server-rendered Jinja2 templates in `web/templates/`, one small CSS
file, no JavaScript framework. Every page works on a phone width.

| Route | Method | Shows / does |
|---|---|---|
| `/` | GET | Each device: name, type, host, last poll time, reachable or not, events in the last hour, high-severity count. Top five suspects of the last two hours. Links to `/report?since=15m`, `2h`, `24h`, `7d`. The inventory load error if there is one. |
| `/report` | GET | `since=` (`15m`, `2h`, `1d`) or `from=` and `to=` (local ISO times). Suspects, chain, timeline, as the v1 HTML report, plus a Log section listing stored log lines of the window for all devices. |
| `/devices` | GET | Table of devices with edit links and an Add button. |
| `/devices/new` | GET, POST | Form: name, host, device type (select), credential set (select), poll interval, skip (checkboxes from the profile). POST validates, saves, sets `reload`, redirects to `/devices`. |
| `/devices/{name}` | GET, POST | Same form, prefilled. Renaming is editing the name field. |
| `/devices/{name}/delete` | POST | Removes the device from the file. Its history stays in the database. |
| `/devices/{name}/test` | POST | Runs the profile's version command through the transport in a thread and shows version or the redacted error. 30 s budget. |
| `/healthz` | GET | `200` JSON `{"status":"ok","devices":N,"last_poll":"..."}`; `503` when the collector task has died. For the compose healthcheck. |

- POST handlers reject a request whose `Origin` header is present and does
  not match `Host` (403). That is the only protection against a browser on
  the LAN being driven by a hostile page; there is no login by decision.
- Nothing from any credential set is ever rendered. A test asserts that no
  page contains a password value from the test environment.
- Times render in `LOOPWATCH_TZ` (see below). Report forms take and show
  local times.

## Time zone

`LOOPWATCH_TZ` (IANA name, default `UTC`) is resolved once at start with
`zoneinfo`; an unknown name is a startup error with the name in the message.
`render_text(data, tz)` and `render_html(data, tz)` take the zone; the CLI
passes it too, so `loop-watch report` in the container prints office time.
Storage and all comparisons stay UTC.

## Publishing and deployment

- `ci.yml`: on push and pull request, Python 3.12, `pip install .[dev]`,
  `pytest -q`.
- `publish.yml`: on push to `main` and tags `v*`, after the tests, build the
  Dockerfile and push `ghcr.io/patrickb100/loop-watch` with tags `latest`
  (main), `sha-<short>` and the semver tag when present. Uses the workflow's
  `GITHUB_TOKEN` with `packages: write`.
- The package is private by default because the repo is. The owner either
  makes the package public (the image contains the tool only) or adds a
  `ghcr.io` registry in Portainer with a token that has `read:packages`. The
  README documents both.
- `Dockerfile`: `LOOPWATCH_INVENTORY=/data/inventory.yaml`, `EXPOSE 8080`,
  `.dockerignore` excludes tests, docs, `.venv`, `.git`, `.env*`.
- `docker-compose.yml` and the README Portainer section use `network_mode:
  host`, the data volume, `${LOOPWATCH_*}` environment references, the
  healthcheck against `/healthz`, and json-file log rotation. For a plain
  compose host the README keeps `env_file: .env`.

## Error handling

- Unknown `device_type` in the file: inventory load error, shown on the home
  page and in `check-inventory`.
- Web handler exceptions: aiohttp renders a plain 500 page with a short
  message; the collector is unaffected.
- A web server that cannot bind its port stops `watch` with a message naming
  the port: the service is not useful half up.
- Transport and parse errors on the router follow the v1 rules.

## Testing

- `tests/fixtures/routeros7/ccr/*.txt`: hand-written fixtures for the four
  commands and `/system resource print`, anonymised, to be replaced by a
  recording. One test per command; one snapshot test; one test that the
  storm and link-flap detectors fire on a router snapshot pair and the STP
  and MAC detectors return nothing.
- Profiles: `profile_for` for every device type; unknown type raises.
- Inventory: credential sets from env; missing set rejected; `save` then
  `load` round-trip; validation messages; missing file is empty.
- Collector: add, remove and change a device during a run with the fake
  transport and fake clock; a failing reload keeps the old set.
- Web: aiohttp test client. Home with zero devices and with the fixture
  database; report window parsing; device add/edit/delete round-trip into
  the YAML file; test-connection success and failure; `Origin` mismatch
  gives 403; `/healthz` ok and 503; no credential value in any response.
- Time zone: one render test with `Europe/Amsterdam`.
- Diagnosis: scenario with LLDP neighbour on the port; scenario without
  neighbour but with MACs learned on the port before the loop and DHCP
  leases from a router fixture; the two-ports cable case; a window with no
  suspects returns `None`; vendor lookup for a known and an unknown prefix.
- Store: `other_port` column added to a v1 database on `init_schema`.
- No test opens a network connection or needs Docker.

## Risks

- RouterOS output layouts differ between versions and from memory. The
  parse-error path and the recording step exist for this.
- Reload races: a save during a poll. The diff runs between polls and the
  store is locked; the worst case is one dropped poll result.
- Host networking plus no login: whoever reaches the host on the port can
  change the device list. The owner accepted this for the office LAN.
- Private package: the first Portainer pull fails until the visibility or
  registry credential is sorted. Documented in the README.

## Version

`0.2.0`, bumped in its own `chore(release)` commit at the end.
