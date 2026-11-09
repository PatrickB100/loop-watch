# loop-watch design

Date: 2026-11-09
Status: draft for review

## Problem

The office network has an intermittent layer-2 loop. Nobody has found the
cause. Each time, the whole Cisco switch environment is rebooted to recover,
which destroys the evidence. We need a tool that watches the switches
continuously, so that when the next loop happens we can read back which
switch, port and device started it.

## Goal

A small service that runs in Docker on a server in the office network, polls
every Cisco switch over SSH, keeps a history of the state that matters for
loop diagnosis, detects the signs of a loop as they appear, and produces a
report with a timeline and a ranked list of suspect ports.

Success means: after the next loop, running one command gives a report that
names the port the loop entered on, with the evidence behind that claim.

## Assumptions

- Cisco Catalyst switches running IOS or IOS-XE, reachable over SSH with a
  read-only account (privilege level 1 with the needed `show` commands, or
  level 15 if that is all the site has).
- Spanning tree is running. The tool does not change switch configuration.
- Up to a few dozen switches. One Docker container is enough.
- Switch credentials and the switch list are provided in a `.env` file and a
  YAML inventory on the host. They never appear in the repo, in logs or in
  reports.

## Out of scope for version 1

- Syslog receiver. Candidate for version 2; it needs a `logging host` line
  on every switch.
- Alerting (Teams, mail). Version 2.
- Meraki or other cloud-managed switches.
- Making configuration changes on switches, for example shutting a port.

## Architecture

Single Python 3.12 application, packaged as one Docker image, with three
CLI commands sharing one SQLite database.

```
                 +--------------------------------------------+
                 |  loop-watch container                      |
 switches  <--SSH|  collector (one task per switch)           |
                 |      |  raw show output                    |
                 |      v                                     |
                 |  parsers  -> typed snapshots                |
                 |      |                                     |
                 |      v                                     |
                 |  detectors -> events                        |
                 |      |                                     |
                 |      v                                     |
                 |  store (SQLite on a volume)                 |
                 +------|-------------------------------------+
                        v
               report (text / HTML)      snapshot (one-off poll)
```

### Modules

| Module | Responsibility | Depends on |
|---|---|---|
| `inventory` | Load switch list and credentials from YAML and environment | nothing |
| `transport` | Open an SSH session, run a list of commands, return raw text. One real implementation (netmiko) and one fake that replays fixture files | `inventory` |
| `parsers` | Turn raw `show` output into typed snapshot structs | ntc-templates, plus hand-written parsers where templates fall short |
| `model` | Dataclasses for snapshots and events | nothing |
| `detectors` | Compare the new snapshot with the previous one and emit events | `model` |
| `store` | SQLite schema, write snapshots and events, read for reports | `model` |
| `collector` | Scheduling loop: per-switch tasks, intervals, escalation | `transport`, `parsers`, `detectors`, `store` |
| `report` | Timeline and suspect ranking for a time window, rendered as text or one HTML file | `store` |
| `cli` | `watch`, `report`, `snapshot` | all of the above |

Rule: `parsers` and `detectors` take plain data in and give plain data out.
They know nothing about SSH or SQLite, so they are tested with fixtures only.

## Collection

Each poll runs this fixed command set on a switch:

| Command | Used for |
|---|---|
| `show spanning-tree detail` | Topology change count per VLAN, time since last change, port that received the last change, root and port roles |
| `show spanning-tree summary` | Mode, portfast and BPDU guard settings |
| `show mac address-table dynamic` | Which MAC sits on which port and VLAN |
| `show interfaces` | Input and output packet counters, broadcast and multicast counters, errors, link state |
| `show interfaces status` | Admin and link state, speed, VLAN |
| `show cdp neighbors detail` and `show lldp neighbors detail` | Who is connected to which port |
| `show logging` | MACFLAP, STP, storm-control, err-disable and link messages since the last poll |

Timing:

- Normal interval: 60 seconds per switch, switches polled concurrently with a
  cap of 8 sessions at once.
- Escalated interval: 10 seconds on a switch for 10 minutes after any
  detector fires on it or on a switch that is its CDP neighbour.
- SSH connect timeout 15 seconds, command timeout 30 seconds. A failed poll
  is stored as an `unreachable` event for that switch and the next attempt
  follows the normal schedule. Unreachable during a storm is itself
  evidence.
- Counters are stored as raw values; rates are computed in detectors from
  consecutive snapshots, so a missed poll does not corrupt rates.

Load on the switches: one SSH session, eight `show` commands, once a minute.
`show mac address-table` on a large campus switch can be slow; the inventory
lets a switch opt out of specific commands if it ever becomes a problem.

## Detectors

Every event has: time, switch, kind, severity, and where known port, VLAN,
MAC and a short human-readable reason. Detectors run per switch on each new
snapshot, comparing with the previous snapshot of the same switch.

| Kind | Fires when | Severity |
|---|---|---|
| `stp_topology_change` | Topology change count for a VLAN increased. Records the port that received the change. | medium; high if more than 3 in 5 minutes |
| `stp_role_change` | A port changed STP role or state (for example designated to root, or forwarding to blocking) | medium |
| `mac_flap` | The same MAC is on a different port than in the previous snapshot, or a MACFLAP line appears in the log buffer | high |
| `broadcast_storm` | Broadcast plus multicast packets per second on a port exceed 20 times that port's rolling 1-hour median, and at least 1,000 packets per second | high |
| `unexpected_neighbor` | A CDP or LLDP neighbour that is a switch or bridge appears on a port whose STP type is edge or portfast, or a portfast port shows a BPDU received count increasing | high |
| `link_flap` | A port went down and up between polls, or more than 3 LINK-UPDOWN log lines for one port in 5 minutes | low; medium if repeated |
| `err_disable` | A port was err-disabled (BPDU guard, loopback, storm control) | high |
| `unreachable` | SSH connect or command timed out | low; medium if more than one switch at once |

Thresholds are constants in one file with a comment on each. They can be
tuned once we have real data from the office.

## Suspect ranking

The report answers "where did it come from" with a score per (switch, port)
in the chosen time window:

- `unexpected_neighbor` or `err_disable` on the port: +50
- `mac_flap` naming the port: +20 per distinct MAC, capped at 100
- `broadcast_storm` on the port: +30
- `stp_topology_change` received on the port: +10 each, capped at 50
- The port is an uplink to another managed switch in the inventory: score
  halved. Loops enter at edges; uplinks mostly relay the symptoms.

The report shows the top 10 ports with the events behind each score, and
the full timeline. It also draws the topology-change chain: for each
switch, the port the last change arrived on, followed through CDP, so you
can see the path towards the source.

## Storage

SQLite file on a Docker volume, WAL mode. Tables:

- `switches(id, name, host, model, last_seen)`
- `polls(id, switch_id, started_at, finished_at, ok, error)`
- `interfaces(poll_id, name, vlan, link, stp_role, stp_state, stp_type,
  in_pkts, out_pkts, broadcast, multicast, errors, bpdu_rx)`
- `mac_entries(poll_id, vlan, mac, port)`
- `neighbors(poll_id, port, neighbor_name, neighbor_port, capabilities)`
- `stp_vlans(poll_id, vlan, root_id, tc_count, tc_last_port, tc_last_age)`
- `events(id, time, switch_id, kind, severity, port, vlan, mac, reason)`
- `log_lines(poll_id, time, text)` for lines matched as relevant

Retention: raw snapshots 14 days, events and log lines 180 days. A daily job
in `watch` prunes. At one-minute polling for 30 switches this stays well
under a few gigabytes.

## CLI

```
loop-watch watch                       run the collector until stopped
loop-watch report --since 2h           text report to stdout
loop-watch report --from T --to T --html out.html
loop-watch snapshot [switch...]        poll now, print a short diagnosis
loop-watch check-inventory             connect to every switch once, run
                                       `show version`, report failures
```

`snapshot` is for use during an incident: it polls the given switches at
once, writes to the same database, and prints the suspect ranking for the
last 15 minutes.

## Configuration

`inventory.yaml` on the host, mounted read-only:

```yaml
switches:
  - name: sw-core-1
    host: 10.0.0.1
  - name: sw-floor2-a
    host: 10.0.0.12
    skip: [show cdp neighbors detail]   # optional
defaults:
  device_type: cisco_ios
  poll_interval: 60
```

`.env` on the host, never committed: `LOOPWATCH_SSH_USER`,
`LOOPWATCH_SSH_PASSWORD`, optionally `LOOPWATCH_SSH_ENABLE`. The repo ships
`.env.example` with empty values. Nothing from these variables is ever
written to the database, log output or reports.

## Error handling

- Transport errors are caught per switch and become `unreachable` events;
  other switches continue.
- A parser that cannot read a command's output records a `parse_error`
  event with the command name and the first 200 characters of output, and
  the rest of the snapshot is still stored. The tool must degrade, not stop,
  on an unknown IOS version.
- Database write failures stop the service with a clear message; silently
  losing history defeats the purpose.

## Testing

- Fixtures: recorded `show` output for at least two IOS versions, stored
  under `tests/fixtures/<model>/<command>.txt`, with MACs and hostnames
  anonymised.
- Parsers: one test per command per fixture, asserting the typed result.
- Detectors: scenario tests with two or three hand-built snapshots, for
  example "desk switch plugged in" producing `unexpected_neighbor` then
  `mac_flap` then `broadcast_storm`, and the suspect ranking putting the
  right port first.
- Collector: uses the fake transport that replays a directory of fixtures
  per poll, so the whole loop is tested without SSH.
- No test opens a network connection.

## Deployment

`Dockerfile` and `docker-compose.yml`. Compose mounts `inventory.yaml`,
reads `.env`, and keeps the database on a named volume. `restart: unless-
stopped`. Logs to stdout at INFO, with each poll result on one line.

## Risks

- During a real storm the switch CPU is saturated and SSH may fail. The
  value of the tool is the history from the minutes before. Escalation to
  10-second polling helps catch the first seconds.
- The read-only account must be restricted to `show` commands. The tool
  never sends anything else, but the credentials should not allow it
  either.
- ntc-templates coverage varies by IOS version. The parse-error path and
  fixture tests exist for this reason.
- Polling itself must never be the load problem. The command set is fixed
  and small, and the inventory can skip heavy commands per switch.

## Version 2 candidates

- Syslog receiver on UDP 514 for MACFLAP and STP messages in real time.
- Teams webhook alert when severity high events appear.
- Automatic diagnosis text: "Loop most likely entered on sw-floor2-a
  Gi1/0/14 at 09:42:10 via an unmanaged switch with MACs ...".
