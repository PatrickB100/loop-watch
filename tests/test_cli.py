from datetime import timedelta

import pytest
from click.testing import CliRunner

from loopwatch.cli import main, parse_since
from loopwatch.model import Event
from loopwatch.store import Store
from tests.conftest import FIXTURES, NOW


@pytest.fixture
def inventory_file(tmp_path):
    p = tmp_path / "inventory.yaml"
    p.write_text("switches:\n  - {name: sw-a, host: 10.0.0.5}\n  - {name: sw-core-1, host: 10.0.0.1}\n")
    return p


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LOOPWATCH_SSH_USER", "ro")
    monkeypatch.setenv("LOOPWATCH_SSH_PASSWORD", "hunter2")
    monkeypatch.delenv("LOOPWATCH_SSH_ENABLE", raising=False)


@pytest.fixture
def fixtures_root():
    return FIXTURES / "ios15"


@pytest.fixture
def fixtures_root_with_unreachable(tmp_path, fixtures_root):
    import shutil

    root = tmp_path / "fx"
    shutil.copytree(fixtures_root, root)
    (root / "sw-core-1" / "UNREACHABLE").touch()
    return root


@pytest.fixture
def seeded_db_path(tmp_path, snap_a):
    path = tmp_path / "seed.sqlite"
    with Store(path) as s:
        s.init_schema()
        s.record_poll("sw-a", "10.0.0.5", NOW - timedelta(minutes=30), NOW - timedelta(minutes=30), snap_a, None)
        s.write_events([Event(NOW - timedelta(minutes=20), "sw-a", "broadcast_storm", "high", "storm", port="Gi1/0/14")])
    return path


def test_snapshot_with_fixtures_prints_suspects(tmp_path, fixtures_root, inventory_file, env):
    r = CliRunner().invoke(main, [
        "--inventory", str(inventory_file), "--db", str(tmp_path / "db.sqlite"),
        "snapshot", "--transport-fixtures", str(fixtures_root), "--record", str(tmp_path / "rec"),
    ])
    assert r.exit_code == 0, r.output
    assert "Suspects" in r.output and "sw-a Gi1/0/14" in r.output
    assert (tmp_path / "rec" / "sw-a" / "show_spanning-tree_detail.txt").exists()
    assert (tmp_path / "rec" / "sw-core-1" / "show_logging.txt").exists()
    with Store(tmp_path / "db.sqlite") as s:
        assert s.switches() == ["sw-a", "sw-core-1"]


def test_snapshot_single_switch(tmp_path, fixtures_root, inventory_file, env):
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "--db", str(tmp_path / "db.sqlite"),
                                  "snapshot", "sw-core-1", "--transport-fixtures", str(fixtures_root)])
    assert r.exit_code == 0, r.output
    with Store(tmp_path / "db.sqlite") as s:
        assert s.switches() == ["sw-core-1"]


def test_snapshot_unknown_switch_fails(tmp_path, fixtures_root, inventory_file, env):
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "--db", str(tmp_path / "db.sqlite"),
                                  "snapshot", "sw-nope", "--transport-fixtures", str(fixtures_root)])
    assert r.exit_code != 0 and "sw-nope" in r.output


def test_report_since_parses_and_prints(seeded_db_path):
    r = CliRunner().invoke(main, ["--db", str(seeded_db_path), "report", "--since", "2h"])
    assert r.exit_code == 0, r.output
    assert "Suspects" in r.output and "Timeline" in r.output


def test_report_from_to(seeded_db_path):
    since = (NOW - timedelta(hours=1)).isoformat()
    until = NOW.isoformat()
    r = CliRunner().invoke(main, ["--db", str(seeded_db_path), "report", "--from", since, "--to", until])
    assert r.exit_code == 0, r.output
    assert "broadcast_storm" in r.output


def test_report_html_writes_file(tmp_path, seeded_db_path):
    out = tmp_path / "report.html"
    r = CliRunner().invoke(main, ["--db", str(seeded_db_path), "report", "--since", "1d", "--html", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists() and "<table" in out.read_text()


def test_report_rejects_bad_since(seeded_db_path):
    r = CliRunner().invoke(main, ["--db", str(seeded_db_path), "report", "--since", "soon"])
    assert r.exit_code != 0


@pytest.mark.parametrize("text,seconds", [("30m", 1800), ("2h", 7200), ("1d", 86400)])
def test_parse_since(text, seconds):
    assert parse_since(text) == timedelta(seconds=seconds)


def test_check_inventory_reports_failure(inventory_file, fixtures_root_with_unreachable, env):
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "check-inventory",
                                  "--transport-fixtures", str(fixtures_root_with_unreachable)])
    assert r.exit_code == 1, r.output
    assert "sw-a" in r.output and "ok" in r.output and "FAIL" in r.output and "sw-core-1" in r.output


def test_check_inventory_all_ok(inventory_file, fixtures_root, env):
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "check-inventory",
                                  "--transport-fixtures", str(fixtures_root)])
    assert r.exit_code == 0, r.output
    assert "15.2(7)E7" in r.output


def test_output_never_contains_password(tmp_path, inventory_file, fixtures_root_with_unreachable, env):
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "--db", str(tmp_path / "db.sqlite"),
                                  "snapshot", "--transport-fixtures", str(fixtures_root_with_unreachable)])
    assert r.exit_code == 0, r.output
    assert "hunter2" not in r.output and "unreachable" in r.output


def test_missing_credentials_is_a_clear_error(tmp_path, inventory_file, fixtures_root, monkeypatch):
    monkeypatch.delenv("LOOPWATCH_SSH_USER", raising=False)
    monkeypatch.delenv("LOOPWATCH_SSH_PASSWORD", raising=False)
    r = CliRunner().invoke(main, ["--inventory", str(inventory_file), "--db", str(tmp_path / "db.sqlite"),
                                  "snapshot", "--transport-fixtures", str(fixtures_root)])
    assert r.exit_code != 0 and "LOOPWATCH_SSH_USER" in r.output


def test_help_lists_commands():
    r = CliRunner().invoke(main, ["--help"])
    assert r.exit_code == 0
    for cmd in ("watch", "report", "snapshot", "check-inventory"):
        assert cmd in r.output


def test_watch_no_web_flag_and_port_option_exist():
    out = CliRunner().invoke(main, ["watch", "--help"]).output
    assert "--no-web" in out and "--port" in out and "LOOPWATCH_WEB_PORT" in out
