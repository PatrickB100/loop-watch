# loop-watch

Watches Cisco Catalyst switches and a MikroTik router over SSH, records the
state that matters for layer-2 loop diagnosis, and tells you which switch
port a loop entered on and what was plugged in there. It runs around the
clock so that the next time the office network loops, the minutes before the
storm are already on disk, and a web page shows the answer.

Design: [v1](docs/superpowers/specs/2026-11-09-loop-watch-design.md),
[v2](docs/superpowers/specs/2026-11-09-loop-watch-v2-design.md).

## What it watches

Every 60 seconds per device (10 seconds while something is happening):
spanning-tree topology changes and the port they came from, MAC addresses
moving between ports, broadcast and multicast rates against each port's own
baseline, switches or BPDUs appearing on portfast (edge) ports, link flaps,
err-disabled ports, and devices that stop answering. The MikroTik router
contributes its log, interface rates, neighbours and DHCP leases, so MACs
in the report come with a hostname and IP address.

## Requirements

- Catalyst switches running IOS or IOS-XE, reachable over SSH, with a
  read-only account. See *Preparing the switches* below.
- Optionally a MikroTik RouterOS 7 device with a user in the `read` group.
- Docker on a host inside the office network. The web page has no login:
  keep that host reachable from the office LAN only.

## Quick start with Portainer

1. In Portainer, add a stack and paste [docker-compose.yml](docker-compose.yml)
   as it is.
2. In the stack's *Environment variables*, add the names from
   [.env.example](.env.example) with your values: at least
   `LOOPWATCH_SSH_USER` and `LOOPWATCH_SSH_PASSWORD`, plus
   `LOOPWATCH_MIKROTIK_USER` and `LOOPWATCH_MIKROTIK_PASSWORD` for the
   router.
3. The image lives at `ghcr.io/patrickb100/loop-watch`. The repository is
   private, so either make the package public in its GitHub settings (the
   image holds the tool only, no credentials or inventory) or add a
   `ghcr.io` registry in Portainer with a personal access token that has
   `read:packages`.
4. Deploy, then open `http://<docker-host>:8080`, go to *Devices* and add
   your switches and the router. The collector picks them up at once.

With plain `docker compose`: copy `.env.example` to `.env`, fill it in,
uncomment `env_file: .env` in the compose file, and run
`docker compose up -d`.

The stack uses `network_mode: host`, so the page listens on the host's own
port (`LOOPWATCH_WEB_PORT`, default 8080) and there is no port mapping to
change.

## The web page

- **Home**: every device with its last poll and status, the diagnosis and
  the top suspects of the last two hours, quick links to report windows.
- **Report**: the diagnosis, ranked suspect ports, the topology-change
  chain, the timeline and the log lines of any window.
- **Devices**: add, edit, delete and test devices. Credentials are chosen
  by set name only; their values never reach the page.

Times are shown in `LOOPWATCH_TZ` (IANA name, default UTC).

## Reading a report

- **Diagnosis** names the port the loop most likely entered on and what the
  evidence says about it: a bridge seen on an edge port, a wireless-to-wired
  bridge, or a cable between two ports. It lists the device connected there
  (from CDP/LLDP), the MACs that were on that port before the loop (the
  devices physically attached), and the MACs that flapped through it, each
  with vendor, DHCP hostname and IP address when known. Flapping MACs are
  usually victims seen through the loop, not the cause. A hostname that
  shows up behind two MACs on both sides is a laptop bridging its Wi-Fi and
  wired adapters.
- **Suspects** scores each port from the events against it. A switch on an
  edge port or an err-disabled port counts most, then MAC flaps per distinct
  MAC, broadcast storms, and topology changes. Uplinks between managed
  devices and ports with an access point score half.
- **Topology change chain** follows "last topology change from port X" from
  switch to switch through CDP towards the edge.
- **Timeline** and **Log** list every event and every stored log line in
  the window. Log lines carry the device's own stamp, shown as written.

## Preparing the switches

The tool only reads. These settings make sure the evidence exists and the
account can read it.

```
! 1. Read-only account and SSH v2
username loopwatch privilege 1 secret <password>
ip ssh version 2
line vty 0 15
 login local
 transport input ssh

! 2. A log buffer that survives a storm, with usable timestamps
logging buffered 128000 informational
service timestamps log datetime msec show-timezone
ntp server <your ntp>

! 3. Log MAC moves and see neighbours (access points, desk switches)
mac address-table notification mac-move
lldp run
```

Privilege 1 can run every `show` command used here on current IOS. If
`check-inventory` or the page's *Test connection* reports `% Invalid input`,
give the account privilege 15 and set `LOOPWATCH_SSH_ENABLE`. Each switch's
`hostname` must equal its name in the inventory, because uplinks are found
by matching CDP and LLDP identities against inventory names.

Optional protection that also gives the tool a clear `err_disable` event:

```
spanning-tree portfast default
spanning-tree portfast bpduguard default
errdisable recovery cause bpduguard
errdisable recovery interval 300
```

This stops a wired loop at the edge in under a second. It does nothing
against a wireless bridge, which sends no BPDUs; those are found through MAC
flaps between the access-point port and the bridging device's port, so mark
your access-point ports on the device page or with `lldp run`.

## The MikroTik router

Create a user in the `read` group and put its name and password in
`LOOPWATCH_MIKROTIK_USER` and `LOOPWATCH_MIKROTIK_PASSWORD`. Add the router
on the device page with type `mikrotik_routeros` and credential set
`mikrotik`. The tool runs `/interface print`, `/interface ethernet print
stats`, `/ip neighbor print detail`, `/log print` and `/ip dhcp-server lease
print detail`. The lease table is what turns MACs into hostnames.

The RouterOS parsers were written from documentation, not from a CCR2116.
Run `loop-watch snapshot ccr --record ./captures` once (see below) and
check the report; a layout the parser cannot read appears as a
`parse_error` event naming the command.

## Commands

```bash
loop-watch check-inventory                  # connect to every device once, show its software version
loop-watch watch [--port 8080] [--no-web]   # collector plus web page (what the container does)
loop-watch report --since 2h                # text report
loop-watch report --from 2026-11-09T09:30 --to 2026-11-09T10:00 --html loop.html
loop-watch snapshot                         # poll everything now, print the suspects of the last 15 minutes
loop-watch snapshot sw-floor2-a --record ./captures
```

Inside the container, prefix with `docker exec loop-watch`.

## Recording real device output

The parsers are tested against recorded output in `tests/fixtures/`. To add
your own devices' output, run `loop-watch snapshot <device> --record DIR`
and copy `DIR/<device>/` into `tests/fixtures/<platform>/<device>/`. Replace
real MAC addresses, hostnames and IP addresses before committing.

## Thresholds

Detector thresholds are constants with comments in
`src/loopwatch/detectors/storm.py` (storm factor and minimum rate),
`src/loopwatch/detectors/stp.py` (topology change escalation) and
`src/loopwatch/detectors/links.py` (link flap count). Poll intervals,
escalation and the inventory check interval live in
`src/loopwatch/collector.py`; ranking weights in
`src/loopwatch/report/ranking.py`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

No test opens a network connection. GitHub Actions runs the suite on every
push and publishes the image from `main` and `v*` tags.
