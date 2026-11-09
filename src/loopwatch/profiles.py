"""One profile per device family: which commands to run, how to parse them, how to read the version."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from loopwatch.inventory import COMMANDS, InventoryError
from loopwatch.model import Snapshot
from loopwatch.parsers.mikrotik import MIKROTIK_COMMANDS, parse_mikrotik_snapshot
from loopwatch.parsers.snapshot import parse_snapshot


@dataclass(frozen=True)
class Profile:
    family: str
    device_types: frozenset[str]  # netmiko device_type values that map here
    commands: tuple[str, ...]
    parse: Callable[[str, Mapping[str, str], datetime], Snapshot]
    version_command: str
    version_pattern: str  # regex with one group


CISCO_IOS = Profile(
    family="cisco_ios",
    device_types=frozenset({"cisco_ios", "cisco_xe"}),
    commands=COMMANDS,
    parse=parse_snapshot,
    version_command="show version",
    version_pattern=r"Version ([^,\s]+)",
)

MIKROTIK_ROUTEROS = Profile(
    family="mikrotik_routeros",
    device_types=frozenset({"mikrotik_routeros"}),
    commands=MIKROTIK_COMMANDS,
    parse=parse_mikrotik_snapshot,
    version_command="/system resource print",
    version_pattern=r"version:\s*(\S+)",
)

PROFILES: tuple[Profile, ...] = (CISCO_IOS, MIKROTIK_ROUTEROS)


def profile_for(device_type: str) -> Profile:
    for profile in PROFILES:
        if device_type in profile.device_types:
            return profile
    raise InventoryError(f"unknown device_type {device_type!r}")


def version_of(profile: Profile, text: str) -> str | None:
    m = re.search(profile.version_pattern, text)
    return m.group(1) if m else None
