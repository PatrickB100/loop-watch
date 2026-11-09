from pathlib import Path

import pytest

from loopwatch.inventory import Credentials, Inventory, InventoryError, load_inventory

FIXTURE = Path(__file__).parent / "fixtures" / "inventory" / "basic.yaml"
ENV = {"LOOPWATCH_SSH_USER": "ro", "LOOPWATCH_SSH_PASSWORD": "pw"}


def test_load_inventory_applies_defaults_and_skip():
    inv = load_inventory(FIXTURE, ENV)
    assert [s.name for s in inv.switches] == ["sw-core-1", "sw-floor2-a"]
    assert inv.by_name("sw-core-1").poll_interval == 60
    assert inv.by_name("sw-core-1").device_type == "cisco_ios"
    assert "show cdp neighbors detail" not in inv.by_name("sw-floor2-a").commands
    assert len(inv.by_name("sw-core-1").commands) == 8
    assert inv.credentials.user == "ro" and inv.credentials.enable is None


def test_missing_credentials_raises():
    with pytest.raises(InventoryError):
        load_inventory(FIXTURE, {})


def test_duplicate_names_raise(tmp_path):
    p = tmp_path / "inv.yaml"
    p.write_text("switches:\n  - {name: a, host: 1.1.1.1}\n  - {name: a, host: 2.2.2.2}\n")
    with pytest.raises(InventoryError):
        load_inventory(p, ENV)


def test_missing_host_raises(tmp_path):
    p = tmp_path / "inv.yaml"
    p.write_text("switches:\n  - {name: a}\n")
    with pytest.raises(InventoryError):
        load_inventory(p, ENV)


def test_credentials_repr_hides_password():
    creds = Credentials("ro", "hunter2", None)
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in str(Inventory((), creds))
