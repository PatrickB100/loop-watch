import os
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from loopwatch.ports import normalize_port

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

# Every LOOPWATCH_<NAME>_USER with a LOOPWATCH_<NAME>_PASSWORD defines the credential set <name>.
ENV_SET = re.compile(r"^LOOPWATCH_([A-Z0-9]+)_USER$")
ENV_USER = "LOOPWATCH_SSH_USER"
ENV_PASSWORD = "LOOPWATCH_SSH_PASSWORD"
ENV_ENABLE = "LOOPWATCH_SSH_ENABLE"
DEFAULT_SET = "ssh"
DEFAULT_DEVICE_TYPE = "cisco_ios"
DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 3600

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


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
    device_type: str = DEFAULT_DEVICE_TYPE
    poll_interval: int = DEFAULT_POLL_INTERVAL
    skip: frozenset[str] = frozenset()
    credentials: str = DEFAULT_SET
    ap_ports: frozenset[str] = frozenset()  # ports with an access point, normalised names

    @property
    def commands(self) -> tuple[str, ...]:
        from loopwatch.profiles import profile_for  # profiles imports this module; resolve at call time

        return tuple(c for c in profile_for(self.device_type).commands if c not in self.skip)


@dataclass(frozen=True)
class Inventory:
    switches: tuple[Switch, ...]
    credential_sets: Mapping[str, Credentials]
    defaults: Mapping[str, object] = field(default_factory=dict)

    def by_name(self, name: str) -> Switch:
        for switch in self.switches:
            if switch.name == name:
                return switch
        raise KeyError(name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.switches)

    def credentials_for(self, switch: Switch) -> Credentials:
        return self.credential_sets[switch.credentials]

    def ap_ports(self) -> frozenset[tuple[str, str]]:
        return frozenset((s.name, p) for s in self.switches for p in s.ap_ports)


def credential_sets_from_env(env: Mapping[str, str]) -> dict[str, Credentials]:
    sets: dict[str, Credentials] = {}
    for key, user in env.items():
        m = ENV_SET.match(key)
        if not m or not user:
            continue
        tag = m.group(1)
        password = env.get(f"LOOPWATCH_{tag}_PASSWORD")
        if not password:
            continue
        sets[tag.lower()] = Credentials(user, password, env.get(f"LOOPWATCH_{tag}_ENABLE") or None)
    return sets


def validate_device(switch: Switch, credential_sets: Set[str]) -> list[str]:
    """Human-readable problems with one device; empty when it is fine."""
    from loopwatch.profiles import profile_for

    msgs: list[str] = []
    if not _NAME.match(switch.name):
        msgs.append("Name must be 1-63 characters: letters, digits, dot, dash, underscore")
    if not switch.host or any(c.isspace() for c in switch.host):
        msgs.append("Host must not be empty or contain whitespace")
    try:
        profile = profile_for(switch.device_type)
    except InventoryError:
        profile = None
        msgs.append(f"Unknown device type {switch.device_type!r}")
    if not MIN_POLL_INTERVAL <= switch.poll_interval <= MAX_POLL_INTERVAL:
        msgs.append(f"Poll interval must be between {MIN_POLL_INTERVAL} and {MAX_POLL_INTERVAL} seconds")
    for command in sorted(switch.skip):
        if profile is None:
            msgs.append(f"{command!r} cannot be checked against an unknown device type")
        elif command not in profile.commands:
            msgs.append(f"{command!r} is not a command of this device type")
    if switch.credentials not in credential_sets:
        tag = switch.credentials.upper()
        msgs.append(f"Credential set {switch.credentials!r} is not defined (LOOPWATCH_{tag}_USER / LOOPWATCH_{tag}_PASSWORD)")
    return msgs


def _entry(entry: Mapping[str, object], defaults: Mapping[str, object]) -> Switch:
    name = entry.get("name")
    host = entry.get("host")
    if not name or not host:
        raise InventoryError(f"switch entry needs name and host: {dict(entry)!r}")
    return Switch(
        name=str(name),
        host=str(host),
        device_type=str(entry.get("device_type", defaults.get("device_type", DEFAULT_DEVICE_TYPE))),
        poll_interval=int(entry.get("poll_interval", defaults.get("poll_interval", DEFAULT_POLL_INTERVAL))),  # type: ignore[call-overload]
        skip=frozenset(str(c) for c in (entry.get("skip") or ())),  # type: ignore[union-attr]
        credentials=str(entry.get("credentials", defaults.get("credentials", DEFAULT_SET))),
        ap_ports=frozenset(normalize_port(str(p)) for p in (entry.get("ap_ports") or ())),  # type: ignore[union-attr]
    )


def parse_inventory(path: Path) -> tuple[list[Switch], Mapping[str, object]]:
    """Read the file without validating devices, so an editor can open and fix a broken entry.
    A missing file is an empty list. Raises InventoryError for unreadable YAML, entries without
    name and host, and duplicate names."""
    path = Path(path)
    if not path.exists():
        return [], {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise InventoryError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(data, Mapping):
        raise InventoryError(f"{path.name} must be a mapping with a 'switches' list")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise InventoryError("'defaults' must be a mapping")
    entries = data.get("switches") or []
    if not isinstance(entries, list):
        raise InventoryError("'switches' must be a list of devices")
    switches: list[Switch] = []
    seen: set[str] = set()
    for i, raw in enumerate(entries, 1):
        if not isinstance(raw, Mapping):
            raise InventoryError(f"switches entry {i} must be a mapping with name and host, got {raw!r}")
        try:
            switch = _entry(raw, defaults)
        except (TypeError, ValueError, AttributeError) as exc:
            raise InventoryError(f"switches entry {i} ({raw.get('name', '?')}): {exc}") from None
        if switch.name in seen:
            raise InventoryError(f"duplicate switch name: {switch.name}")
        seen.add(switch.name)
        switches.append(switch)
    return switches, defaults


def load_inventory(path: Path, env: Mapping[str, str]) -> Inventory:
    """Read and validate the device list; a missing file is an empty inventory. Credentials come
    from `env` only."""
    sets = credential_sets_from_env(env)
    switches, defaults = parse_inventory(path)
    for switch in switches:
        problems = validate_device(switch, sets.keys())
        if problems:
            raise InventoryError(f"device {switch.name!r}: " + "; ".join(problems))
    return Inventory(tuple(switches), sets, defaults)


def save_inventory(path: Path, switches: Sequence[Switch], defaults: Mapping[str, object]) -> None:
    """Write the device list atomically (temp file + rename in the same directory). Never credentials."""
    path = Path(path)
    default_type = defaults.get("device_type", DEFAULT_DEVICE_TYPE)
    default_interval = defaults.get("poll_interval", DEFAULT_POLL_INTERVAL)
    default_set = defaults.get("credentials", DEFAULT_SET)
    entries: list[dict[str, object]] = []
    for s in switches:
        entry: dict[str, object] = {"name": s.name, "host": s.host}
        if s.device_type != default_type:
            entry["device_type"] = s.device_type
        if s.credentials != default_set:
            entry["credentials"] = s.credentials
        if s.poll_interval != default_interval:
            entry["poll_interval"] = s.poll_interval
        if s.skip:
            entry["skip"] = sorted(s.skip)
        if s.ap_ports:
            entry["ap_ports"] = sorted(s.ap_ports)
        entries.append(entry)
    data: dict[str, object] = {"switches": entries}
    if defaults:
        data["defaults"] = dict(defaults)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    os.replace(tmp, path)
