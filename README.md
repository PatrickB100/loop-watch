# loop-watch

Watches Cisco Catalyst switches over SSH, records the state that matters for
layer-2 loop diagnosis, and reports which switch and port a loop entered on.
It runs around the clock so that the next time the office network loops, the
minutes before the storm are already on disk.

Design: [docs/superpowers/specs/2026-11-09-loop-watch-design.md](docs/superpowers/specs/2026-11-09-loop-watch-design.md).

## What it watches

Every 60 seconds per switch (10 seconds while something is happening):
spanning-tree topology changes and the port they came from, MAC addresses
moving between ports, broadcast and multicast rates against each port's own
baseline, switches or BPDUs appearing on portfast (edge) ports, link flaps,
err-disabled ports, and switches that stop answering.

## Requirements

- Catalyst switches running IOS or IOS-XE, reachable over SSH.
- A read-only account. The tool only sends `terminal length 0` and `show`
  commands; the account should not be able to do more than that either.
- Docker on a host inside the office network.

## Setup

```bash
cp .env.example .env            # fill in LOOPWATCH_SSH_USER and LOOPWATCH_SSH_PASSWORD
cp inventory.example.yaml inventory.yaml   # list your switches
docker compose build
docker compose run --rm loop-watch loop-watch check-inventory
docker compose up -d
```

`.env` holds the credentials and is ignored by git. Never put them in
`inventory.yaml`.

## Commands

```bash
loop-watch check-inventory                  # connect to every switch once, show its IOS version
loop-watch watch                            # run the collector (what the container does)
loop-watch report --since 2h                # text timeline and ranked suspect ports
loop-watch report --from 2026-11-09T09:30:00 --to 2026-11-09T10:00:00 --html loop.html
loop-watch snapshot                         # poll everything now, print suspects of the last 15 minutes
loop-watch snapshot sw-floor2-a --record ./captures
```

Inside the container, prefix with `docker compose exec loop-watch`, for example:

```bash
docker compose exec loop-watch loop-watch report --since 1h
```

## Reading a report

- **Suspects** scores each switch port from the events against it. A switch
  on an edge port or an err-disabled port counts most, then MAC flaps per
  distinct MAC, broadcast storms, and topology changes. Uplinks between
  managed switches score half, because loops enter at edges.
- **Topology change chain** follows "last topology change from port X" from
  switch to switch through CDP. The last hop points at the edge where it
  entered.
- **Timeline** lists every event in the window. Events found in a switch's
  log buffer are stamped with the poll time; the switch's own timestamp is
  the first field of the reason. Switch clocks are read as written, so a
  switch logging in CET shows CET there.

Set switch clocks to UTC where you can (`clock timezone UTC 0`), and keep
`service timestamps log datetime msec` on. Switches that stamp logs with
uptime still work, but their log lines carry no time.

## Recording real switch output

The parsers are tested against recorded output in `tests/fixtures/`. To add
your own switches' output, run `loop-watch snapshot <switch> --record DIR`
and copy `DIR/<switch>/` into `tests/fixtures/<platform>/<switch>/`. Replace
real MAC addresses, hostnames and IP addresses before committing.

## Thresholds

Detector thresholds are constants with comments in
`src/loopwatch/detectors/storm.py` (storm factor and minimum rate),
`src/loopwatch/detectors/stp.py` (topology change escalation) and
`src/loopwatch/detectors/links.py` (link flap count). Poll intervals and
escalation live in `src/loopwatch/collector.py`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

No test opens a network connection.
