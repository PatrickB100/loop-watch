import pytest

from loopwatch.inventory import COMMANDS, InventoryError, Switch
from loopwatch.profiles import CISCO_IOS, profile_for, version_of


def test_cisco_types_share_one_profile():
    assert profile_for("cisco_ios") is CISCO_IOS and profile_for("cisco_xe") is CISCO_IOS
    assert CISCO_IOS.commands == COMMANDS


def test_unknown_device_type_raises():
    with pytest.raises(InventoryError, match="unknown device_type 'junos'"):
        profile_for("junos")


def test_switch_commands_follow_profile_minus_skip():
    sw = Switch(name="a", host="h", skip=frozenset({"show logging"}))
    assert sw.commands == tuple(c for c in COMMANDS if c != "show logging")


def test_version_of_cisco():
    assert version_of(CISCO_IOS, "Cisco IOS Software, Version 15.2(7)E8, RELEASE") == "15.2(7)E8"
    assert version_of(CISCO_IOS, "garbage") is None
