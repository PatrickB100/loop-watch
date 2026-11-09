from datetime import datetime, timezone
from pathlib import Path

import pytest

from loopwatch.inventory import COMMANDS, Credentials, Inventory, Switch

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def ios15_root() -> Path:
    return FIXTURES / "ios15"


@pytest.fixture
def sw_a() -> Switch:
    return Switch(name="sw-a", host="10.0.0.5")


@pytest.fixture
def creds() -> Credentials:
    return Credentials("ro", "hunter2", None)


@pytest.fixture
def inv(sw_a, creds) -> Inventory:
    return Inventory((sw_a, Switch(name="sw-core-1", host="10.0.0.1")), creds)


@pytest.fixture
def ios15_raw(ios15_root) -> dict[str, str]:
    from loopwatch.transport import command_slug

    return {c: (ios15_root / "sw-a" / f"{command_slug(c)}.txt").read_text() for c in COMMANDS}
