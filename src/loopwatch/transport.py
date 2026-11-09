from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from loopwatch.inventory import Credentials, Switch


class TransportError(RuntimeError):
    def __init__(self, switch_name: str, message: str) -> None:
        super().__init__(f"{switch_name}: {message}")
        self.switch_name = switch_name
        self.message = message


class Transport(Protocol):
    def run(self, switch: Switch, credentials: Credentials, commands: Sequence[str]) -> dict[str, str]: ...


def command_slug(command: str) -> str:
    return command.replace(" ", "_")


class FakeTransport:
    """Replays recorded output from root/<switch>/<command_slug>.txt. Tests only."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, switch: Switch, credentials: Credentials, commands: Sequence[str]) -> dict[str, str]:
        self.calls.append((switch.name, tuple(commands)))
        folder = self.root / switch.name
        if (folder / "UNREACHABLE").exists():
            raise TransportError(switch.name, "TimeoutError: connection timed out (fixture)")
        out: dict[str, str] = {}
        for command in commands:
            path = folder / f"{command_slug(command)}.txt"
            if not path.exists():
                raise TransportError(switch.name, f"no fixture for {command!r} at {path}")
            out[command] = path.read_text()
        return out


class NetmikoTransport:
    def __init__(self, connect_timeout: int = 15, command_timeout: int = 30) -> None:
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    @staticmethod
    def _wrap(switch_name: str, exc: BaseException, password: str) -> TransportError:
        text = str(exc).replace(password, "***") if password else str(exc)
        return TransportError(switch_name, f"{type(exc).__name__}: {text}")

    def run(self, switch: Switch, credentials: Credentials, commands: Sequence[str]) -> dict[str, str]:
        from netmiko import ConnectHandler  # imported here so tests never load netmiko

        out: dict[str, str] = {}
        try:
            with ConnectHandler(
                device_type=switch.device_type,
                host=switch.host,
                username=credentials.user,
                password=credentials.password,
                secret=credentials.enable or "",
                conn_timeout=self.connect_timeout,
                read_timeout_override=self.command_timeout,
            ) as conn:
                if credentials.enable:
                    conn.enable()
                conn.send_command("terminal length 0", read_timeout=self.command_timeout)
                for command in commands:
                    out[command] = conn.send_command(command, read_timeout=self.command_timeout)
        except Exception as exc:  # netmiko raises many exception types; all become TransportError
            raise self._wrap(switch.name, exc, credentials.password) from None
        return out
