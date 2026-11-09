from pathlib import Path

import pytest

from loopwatch.inventory import (
    Credentials,
    Inventory,
    InventoryError,
    Switch,
    credential_sets_from_env,
    load_inventory,
    save_inventory,
    validate_device,
)

FIXTURE = Path(__file__).parent / "fixtures" / "inventory" / "basic.yaml"
ENV = {"LOOPWATCH_SSH_USER": "ro", "LOOPWATCH_SSH_PASSWORD": "pw"}


def test_load_inventory_applies_defaults_and_skip():
    inv = load_inventory(FIXTURE, ENV)
    assert [s.name for s in inv.switches] == ["sw-core-1", "sw-floor2-a"]
    assert inv.by_name("sw-core-1").poll_interval == 60
    assert inv.by_name("sw-core-1").device_type == "cisco_ios"
    assert "show cdp neighbors detail" not in inv.by_name("sw-floor2-a").commands
    assert len(inv.by_name("sw-core-1").commands) == 8
    creds = inv.credentials_for(inv.by_name("sw-core-1"))
    assert creds.user == "ro" and creds.enable is None


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


def test_credential_sets_from_env_names_pairs_only():
    env = {"LOOPWATCH_SSH_USER": "a", "LOOPWATCH_SSH_PASSWORD": "b", "LOOPWATCH_SSH_ENABLE": "e",
           "LOOPWATCH_MIKROTIK_USER": "m", "LOOPWATCH_MIKROTIK_PASSWORD": "n",
           "LOOPWATCH_LAB_USER": "no-password", "OTHER": "x"}
    sets = credential_sets_from_env(env)
    assert set(sets) == {"ssh", "mikrotik"}
    assert sets["ssh"].enable == "e" and sets["mikrotik"].enable is None


def test_load_resolves_credential_set_and_ap_ports(tmp_path):
    p = tmp_path / "i.yaml"
    p.write_text("switches:\n  - {name: ccr, host: 10.0.0.254, device_type: cisco_xe, credentials: mikrotik}\n"
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
    with pytest.raises(InventoryError, match=r"(?i)credential set 'lab'.*LOOPWATCH_LAB_USER"):
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
    env = {"LOOPWATCH_SSH_USER": "a", "LOOPWATCH_SSH_PASSWORD": "b",
           "LOOPWATCH_MIKROTIK_USER": "m", "LOOPWATCH_MIKROTIK_PASSWORD": "n"}
    switches = (Switch(name="sw-a", host="10.0.0.5", skip=frozenset({"show logging"}), ap_ports=frozenset({"Gi1/0/20"})),
                Switch(name="ccr", host="10.0.0.254", device_type="cisco_xe", credentials="mikrotik", poll_interval=30))
    save_inventory(p, switches, {"device_type": "cisco_ios", "poll_interval": 60})
    text = p.read_text()
    assert "LOOPWATCH" not in text and "password" not in text.lower()
    assert load_inventory(p, env).switches == switches
    assert not list(tmp_path.glob("*.tmp*"))  # temp file replaced, not left behind
