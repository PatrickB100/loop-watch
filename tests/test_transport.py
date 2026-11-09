import pytest

from loopwatch.inventory import COMMANDS
from loopwatch.transport import FakeTransport, NetmikoTransport, TransportError, command_slug


def test_fake_transport_returns_all_commands(ios15_root, sw_a, creds):
    out = FakeTransport(ios15_root).run(sw_a, creds, COMMANDS)
    assert set(out) == set(COMMANDS)
    assert "VLAN0010" in out["show spanning-tree detail"]


def test_fake_transport_records_calls(ios15_root, sw_a, creds):
    fake = FakeTransport(ios15_root)
    fake.run(sw_a, creds, COMMANDS[:2])
    assert fake.calls == [("sw-a", COMMANDS[:2])]


def test_fake_transport_missing_file_raises(ios15_root, sw_a, creds):
    with pytest.raises(TransportError):
        FakeTransport(ios15_root).run(sw_a, creds, ["show nothing"])


def test_fake_transport_unreachable(tmp_path, sw_a, creds):
    (tmp_path / "sw-a").mkdir()
    (tmp_path / "sw-a" / "UNREACHABLE").touch()
    with pytest.raises(TransportError):
        FakeTransport(tmp_path).run(sw_a, creds, COMMANDS)


def test_command_slug():
    assert command_slug("show spanning-tree detail") == "show_spanning-tree_detail"


def test_transport_error_redacts_password():
    err = NetmikoTransport._wrap("sw-a", Exception("auth failed for pw hunter2"), "hunter2")
    assert isinstance(err, TransportError)
    assert "hunter2" not in str(err) and "sw-a" in str(err) and "Exception" in str(err)


def test_transport_error_redacts_enable_secret_too():
    err = NetmikoTransport._wrap("sw-a", Exception("secret s3cret and pw hunter2"), "hunter2", "s3cret")
    assert "s3cret" not in str(err) and "hunter2" not in str(err)
    err = NetmikoTransport._wrap("sw-a", Exception("x"), "hunter2", None)
    assert str(err) == "sw-a: Exception: x"


def test_netmiko_transport_sends_only_the_requested_commands(monkeypatch, sw_a, creds):
    import sys
    import types

    sent: list[str] = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def enable(self):
            pass

        def send_command(self, cmd, **kw):
            sent.append(cmd)
            return f"out of {cmd}"

    fake = types.ModuleType("netmiko")
    fake.ConnectHandler = lambda **kw: FakeConn()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "netmiko", fake)
    out = NetmikoTransport().run(sw_a, creds, ["show version"])
    assert sent == ["show version"] and out == {"show version": "out of show version"}
