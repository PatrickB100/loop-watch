from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

COMMANDS: tuple[str, ...] = (
    "show spanning-tree detail",
    "show spanning-tree summary",
    "show mac address-table dynamic",
    "show interfaces",
    "show interfaces status",
    "show cdp neighbors detail",
    "show lldp neighbors detail",
    "show logging",
)

ENV_USER = "LOOPWATCH_SSH_USER"
ENV_PASSWORD = "LOOPWATCH_SSH_PASSWORD"
ENV_ENABLE = "LOOPWATCH_SSH_ENABLE"


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str = field(repr=False)
    enable: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"Credentials(user={self.user!r}, password='***')"


@dataclass(frozen=True)
class Switch:
    name: str
    host: str
    device_type: str = "cisco_ios"
    poll_interval: int = 60
    skip: frozenset[str] = frozenset()

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(c for c in COMMANDS if c not in self.skip)


@dataclass(frozen=True)
class Inventory:
    switches: tuple[Switch, ...]
    credentials: Credentials

    def by_name(self, name: str) -> Switch:
        for switch in self.switches:
            if switch.name == name:
                return switch
        raise KeyError(name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.switches)


def load_inventory(path: Path, env: Mapping[str, str]) -> Inventory:
    user = env.get(ENV_USER)
    password = env.get(ENV_PASSWORD)
    if not user or not password:
        raise InventoryError(f"{ENV_USER} and {ENV_PASSWORD} must be set")
    credentials = Credentials(user, password, env.get(ENV_ENABLE) or None)

    data = yaml.safe_load(Path(path).read_text()) or {}
    defaults = data.get("defaults") or {}
    switches: list[Switch] = []
    seen: set[str] = set()
    for entry in data.get("switches") or []:
        name = entry.get("name")
        host = entry.get("host")
        if not name or not host:
            raise InventoryError(f"switch entry needs name and host: {entry!r}")
        if name in seen:
            raise InventoryError(f"duplicate switch name: {name}")
        seen.add(name)
        switches.append(
            Switch(
                name=str(name),
                host=str(host),
                device_type=str(entry.get("device_type", defaults.get("device_type", "cisco_ios"))),
                poll_interval=int(entry.get("poll_interval", defaults.get("poll_interval", 60))),
                skip=frozenset(entry.get("skip") or []),
            )
        )
    return Inventory(tuple(switches), credentials)
