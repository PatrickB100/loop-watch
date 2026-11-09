import sqlite3
import threading
from collections.abc import Iterable, Set
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loopwatch.model import Event, Host, Interface, LogLine, MacEntry, Neighbor, Snapshot, StpPort, StpVlan

_SCHEMA = """
CREATE TABLE IF NOT EXISTS switches (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, host TEXT NOT NULL, model TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY, switch_id INTEGER NOT NULL REFERENCES switches(id),
    started_at TEXT NOT NULL, finished_at TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT);
CREATE INDEX IF NOT EXISTS polls_switch_time ON polls(switch_id, finished_at);
CREATE TABLE IF NOT EXISTS interfaces (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE, name TEXT NOT NULL, vlan TEXT, link TEXT, speed TEXT,
    in_pkts INTEGER, out_pkts INTEGER, broadcast INTEGER, multicast INTEGER, in_errors INTEGER, out_errors INTEGER);
CREATE INDEX IF NOT EXISTS interfaces_poll_name ON interfaces(poll_id, name);
CREATE TABLE IF NOT EXISTS mac_entries (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE, vlan INTEGER, mac TEXT, port TEXT);
CREATE INDEX IF NOT EXISTS mac_entries_poll_mac ON mac_entries(poll_id, mac);
CREATE TABLE IF NOT EXISTS neighbors (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE, port TEXT, neighbor_name TEXT,
    neighbor_port TEXT, platform TEXT, capabilities TEXT);
CREATE INDEX IF NOT EXISTS neighbors_poll ON neighbors(poll_id);
CREATE TABLE IF NOT EXISTS stp_vlans (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE, vlan INTEGER, root_id TEXT,
    tc_count INTEGER, tc_last_port TEXT, tc_last_age INTEGER);
CREATE INDEX IF NOT EXISTS stp_vlans_poll ON stp_vlans(poll_id);
CREATE TABLE IF NOT EXISTS stp_ports (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE, vlan INTEGER, port TEXT, role TEXT,
    state TEXT, port_type TEXT, bpdu_sent INTEGER, bpdu_received INTEGER);
CREATE INDEX IF NOT EXISTS stp_ports_poll ON stp_ports(poll_id);
CREATE TABLE IF NOT EXISTS log_lines (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE, time TEXT, mnemonic TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS log_lines_poll ON log_lines(poll_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, switch_id INTEGER NOT NULL REFERENCES switches(id),
    kind TEXT NOT NULL, severity TEXT NOT NULL, port TEXT, vlan INTEGER, mac TEXT, reason TEXT NOT NULL,
    other_port TEXT);
CREATE INDEX IF NOT EXISTS events_time ON events(time);
CREATE TABLE IF NOT EXISTS hosts (
    mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, source TEXT, seen_at TEXT);
"""

# Columns added after v1; init_schema adds them to an existing database.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("events", "other_port", "ALTER TABLE events ADD COLUMN other_port TEXT"),
)

def _iso(dt: datetime | None) -> str | None:
    """ISO text in UTC: SQLite compares these as strings, so every stored and queried value must
    carry the same offset."""
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _locked(method):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


class Store:
    """SQLite store. One connection, shared across threads and serialised by a lock: the
    collector writes from asyncio.to_thread workers and prunes from the event-loop thread."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    @_locked
    def init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(_SCHEMA)
            for table, column, ddl in _MIGRATIONS:
                columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self.conn.execute(ddl)

    # --- writes -----------------------------------------------------------

    def _switch_id(self, name: str, host: str | None = None, seen: datetime | None = None) -> int:
        with self.conn:
            if host is not None:
                self.conn.execute(
                    "INSERT INTO switches(name, host, last_seen) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET host = excluded.host, "
                    "last_seen = COALESCE(excluded.last_seen, switches.last_seen)",
                    (name, host, _iso(seen)),
                )
            else:
                self.conn.execute("INSERT OR IGNORE INTO switches(name, host) VALUES (?, '')", (name,))
        row = self.conn.execute("SELECT id FROM switches WHERE name = ?", (name,)).fetchone()
        return int(row[0])

    @_locked
    def record_poll(
        self,
        switch: str,
        host: str,
        started_at: datetime,
        finished_at: datetime,
        snapshot: Snapshot | None,
        error: str | None,
        new_log_lines: Iterable[LogLine] | None = None,
    ) -> int:
        """Store one poll. `new_log_lines` limits the stored log lines to those not seen in an
        earlier poll (the snapshot itself always carries the whole buffer); None stores them all."""
        switch_id = self._switch_id(switch, host, finished_at if snapshot else None)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO polls(switch_id, started_at, finished_at, ok, error) VALUES (?, ?, ?, ?, ?)",
                (switch_id, _iso(started_at), _iso(finished_at), 1 if snapshot else 0, error),
            )
            poll_id = int(cur.lastrowid or 0)
            if snapshot is not None:
                lines = snapshot.log_lines if new_log_lines is None else tuple(new_log_lines)
                self._write_snapshot(poll_id, snapshot, lines)
        return poll_id

    def _write_snapshot(self, poll_id: int, s: Snapshot, log_lines: tuple[LogLine, ...]) -> None:
        c = self.conn
        c.executemany(
            "INSERT INTO interfaces VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(poll_id, i.port, i.vlan, i.link, i.speed, i.in_pkts, i.out_pkts, i.in_bcast, i.in_mcast,
              i.in_errors, i.out_errors) for i in s.interfaces],
        )
        c.executemany("INSERT INTO mac_entries VALUES (?,?,?,?)", [(poll_id, m.vlan, m.mac, m.port) for m in s.macs])
        c.executemany(
            "INSERT INTO neighbors VALUES (?,?,?,?,?,?)",
            [(poll_id, n.port, n.name, n.remote_port, n.platform, " ".join(sorted(n.capabilities))) for n in s.neighbors],
        )
        c.executemany(
            "INSERT INTO stp_vlans VALUES (?,?,?,?,?,?)",
            [(poll_id, v.vlan, v.root_id, v.tc_count, v.tc_last_port, v.tc_last_age_s) for v in s.stp_vlans],
        )
        c.executemany(
            "INSERT INTO stp_ports VALUES (?,?,?,?,?,?,?,?)",
            [(poll_id, p.vlan, p.port, p.role, p.state, p.port_type, p.bpdu_sent, p.bpdu_received) for p in s.stp_ports],
        )
        c.executemany(
            "INSERT INTO log_lines VALUES (?,?,?,?)",
            [(poll_id, _iso(line.time), line.mnemonic, line.text) for line in log_lines],
        )

    @_locked
    def write_events(self, events: Iterable[Event]) -> None:
        rows = []
        for e in events:
            rows.append((_iso(e.time), self._switch_id(e.switch), e.kind, e.severity, e.port, e.vlan, e.mac, e.reason,
                         e.other_port))
        with self.conn:
            self.conn.executemany(
                "INSERT INTO events(time, switch_id, kind, severity, port, vlan, mac, reason, other_port) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )

    @_locked
    def upsert_hosts(self, hosts: Iterable[Host], source: str, seen: datetime) -> None:
        """Remember hostname and IP per MAC; the latest sighting wins."""
        with self.conn:
            self.conn.executemany(
                "INSERT INTO hosts(mac, ip, hostname, source, seen_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(mac) DO UPDATE SET ip = excluded.ip, hostname = excluded.hostname, "
                "source = excluded.source, seen_at = excluded.seen_at WHERE excluded.seen_at >= hosts.seen_at",
                [(h.mac, h.ip, h.hostname, source, _iso(seen)) for h in hosts],
            )

    # --- reads ------------------------------------------------------------

    @_locked
    def switches(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT name FROM switches ORDER BY name")]

    @_locked
    def events(self, since: datetime, until: datetime, switch: str | None = None) -> list[Event]:
        sql = ("SELECT e.time, s.name, e.kind, e.severity, e.reason, e.port, e.vlan, e.mac, e.other_port "
               "FROM events e JOIN switches s ON s.id = e.switch_id WHERE e.time >= ? AND e.time <= ?")
        args: list[object] = [_iso(since), _iso(until)]
        if switch is not None:
            sql += " AND s.name = ?"
            args.append(switch)
        sql += " ORDER BY e.time, e.id"
        return [Event(_dt(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in self.conn.execute(sql, args)]

    @_locked
    def hosts_for(self, macs: Set[str]) -> dict[str, Host]:
        if not macs:
            return {}
        marks = ",".join("?" * len(macs))
        rows = self.conn.execute(f"SELECT mac, ip, hostname FROM hosts WHERE mac IN ({marks})", tuple(macs))
        return {r[0]: Host(r[0], r[1], r[2], True) for r in rows}

    @_locked
    def log_lines(self, since: datetime, until: datetime) -> list[tuple[datetime, str, LogLine]]:
        """Stored log lines of every device polled in the window: (polled at, device, line)."""
        rows = self.conn.execute(
            "SELECT p.finished_at, s.name, l.time, l.mnemonic, l.text FROM log_lines l "
            "JOIN polls p ON p.id = l.poll_id JOIN switches s ON s.id = p.switch_id "
            "WHERE p.finished_at >= ? AND p.finished_at <= ? ORDER BY p.finished_at, p.id, l.rowid",
            (_iso(since), _iso(until)),
        )
        return [(_dt(r[0]), r[1], LogLine(_dt(r[2]) if r[2] else None, r[3], r[4])) for r in rows]

    @_locked
    def last_poll(self, switch: str) -> tuple[datetime, bool, str | None] | None:
        row = self.conn.execute(
            "SELECT p.finished_at, p.ok, p.error FROM polls p JOIN switches s ON s.id = p.switch_id "
            "WHERE s.name = ? ORDER BY p.finished_at DESC, p.id DESC LIMIT 1",
            (switch,),
        ).fetchone()
        return (_dt(row[0]), bool(row[1]), row[2]) if row else None

    @_locked
    def mac_ports(self, switch: str, since: datetime, until: datetime) -> dict[str, set[str]]:
        """Every port each MAC was learned on, over the switch's polls in the window."""
        rows = self.conn.execute(
            "SELECT DISTINCT m.mac, m.port FROM mac_entries m JOIN polls p ON p.id = m.poll_id "
            "JOIN switches s ON s.id = p.switch_id WHERE s.name = ? AND p.finished_at >= ? AND p.finished_at <= ?",
            (switch, _iso(since), _iso(until)),
        )
        ports: dict[str, set[str]] = {}
        for mac, port in rows:
            ports.setdefault(mac, set()).add(port)
        return ports

    @_locked
    def counter_history(self, switch: str, since: datetime) -> dict[str, list[tuple[datetime, int]]]:
        rows = self.conn.execute(
            "SELECT p.finished_at, i.name, i.broadcast + i.multicast FROM interfaces i "
            "JOIN polls p ON p.id = i.poll_id JOIN switches s ON s.id = p.switch_id "
            "WHERE s.name = ? AND p.ok = 1 AND p.finished_at >= ? ORDER BY p.finished_at, p.id",
            (switch, _iso(since)),
        )
        history: dict[str, list[tuple[datetime, int]]] = {}
        for finished_at, name, total in rows:
            history.setdefault(name, []).append((_dt(finished_at), int(total)))
        return history

    def _latest_ok_poll(self, switch: str, until: datetime) -> int | None:
        row = self.conn.execute(
            "SELECT p.id FROM polls p JOIN switches s ON s.id = p.switch_id "
            "WHERE s.name = ? AND p.ok = 1 AND p.finished_at <= ? ORDER BY p.finished_at DESC, p.id DESC LIMIT 1",
            (switch, _iso(until)),
        ).fetchone()
        return int(row[0]) if row else None

    @_locked
    def latest_stp_vlans(self, switch: str, until: datetime) -> list[StpVlan]:
        poll_id = self._latest_ok_poll(switch, until)
        if poll_id is None:
            return []
        rows = self.conn.execute(
            "SELECT vlan, root_id, tc_count, tc_last_port, tc_last_age FROM stp_vlans WHERE poll_id = ? ORDER BY vlan",
            (poll_id,),
        )
        return [StpVlan(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    @_locked
    def latest_neighbors(self, switch: str, until: datetime) -> list[Neighbor]:
        poll_id = self._latest_ok_poll(switch, until)
        if poll_id is None:
            return []
        rows = self.conn.execute(
            "SELECT port, neighbor_name, neighbor_port, platform, capabilities FROM neighbors WHERE poll_id = ?",
            (poll_id,),
        )
        return [Neighbor(r[0], r[1], r[2], r[3], frozenset(r[4].split())) for r in rows]

    @_locked
    def latest_snapshot(self, switch: str) -> Snapshot | None:
        """The newest successful poll as a Snapshot (without the STP summary, which is not stored).
        Its log_lines are every line stored for the switch, so they can serve as the seen-set
        after a restart."""
        row = self.conn.execute(
            "SELECT p.id, p.finished_at FROM polls p JOIN switches s ON s.id = p.switch_id "
            "WHERE s.name = ? AND p.ok = 1 ORDER BY p.finished_at DESC, p.id DESC LIMIT 1",
            (switch,),
        ).fetchone()
        if row is None:
            return None
        poll_id, finished_at = int(row[0]), _dt(row[1])
        c = self.conn
        interfaces = tuple(
            Interface(*r) for r in c.execute(
                "SELECT name, link, vlan, speed, in_pkts, out_pkts, broadcast, multicast, in_errors, out_errors "
                "FROM interfaces WHERE poll_id = ?", (poll_id,))
        )
        macs = tuple(MacEntry(*r) for r in c.execute("SELECT vlan, mac, port FROM mac_entries WHERE poll_id = ?", (poll_id,)))
        neighbors = tuple(
            Neighbor(r[0], r[1], r[2], r[3], frozenset(r[4].split())) for r in c.execute(
                "SELECT port, neighbor_name, neighbor_port, platform, capabilities FROM neighbors WHERE poll_id = ?",
                (poll_id,))
        )
        stp_vlans = tuple(StpVlan(*r) for r in c.execute(
            "SELECT vlan, root_id, tc_count, tc_last_port, tc_last_age FROM stp_vlans WHERE poll_id = ?", (poll_id,)))
        stp_ports = tuple(StpPort(*r) for r in c.execute(
            "SELECT vlan, port, role, state, port_type, bpdu_sent, bpdu_received FROM stp_ports WHERE poll_id = ?",
            (poll_id,)))
        log_lines = tuple(
            LogLine(_dt(r[0]) if r[0] else None, r[1], r[2]) for r in c.execute(
                "SELECT l.time, l.mnemonic, l.text FROM log_lines l JOIN polls p ON p.id = l.poll_id "
                "JOIN switches s ON s.id = p.switch_id WHERE s.name = ?", (switch,))
        )
        return Snapshot(switch, finished_at, stp_vlans, stp_ports, None, interfaces, macs, neighbors, log_lines, ())

    # --- maintenance ------------------------------------------------------

    @_locked
    def prune(self, now: datetime, snapshot_days: int = 14, event_days: int = 180) -> None:
        snapshot_cutoff = _iso(now - timedelta(days=snapshot_days))
        event_cutoff = _iso(now - timedelta(days=event_days))
        with self.conn:
            # Child rows go with ON DELETE CASCADE; each child table has a poll_id index for it.
            self.conn.execute("DELETE FROM polls WHERE finished_at < ?", (snapshot_cutoff,))
            self.conn.execute("DELETE FROM events WHERE time < ?", (event_cutoff,))
            self.conn.execute("DELETE FROM hosts WHERE seen_at < ?", (event_cutoff,))
