import re

# Longest names first so "TwentyFiveGigE" is not read as something shorter. Matched case-insensitively.
_PREFIXES = (
    ("TwentyFiveGigE", "Twe"),
    ("TwoGigabitEthernet", "Tw"),
    ("TenGigabitEthernet", "Te"),
    ("FortyGigabitEthernet", "Fo"),
    ("HundredGigE", "Hu"),
    ("AppGigabitEthernet", "Ap"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("Port-channel", "Po"),
    ("Vlan", "Vl"),
)


def normalize_port(name: str) -> str:
    """Return the short IOS spelling of an interface name, e.g. Gi1/0/14."""
    name = name.strip()
    lowered = name.lower()
    for long, short in _PREFIXES:
        if lowered.startswith(long.lower()):
            return short + name[len(long):]
    return name


def normalize_mac(mac: str) -> str:
    """Return a MAC as lowercase IOS dotted form, e.g. aabb.ccdd.eeff."""
    digits = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    return f"{digits[0:4]}.{digits[4:8]}.{digits[8:12]}"
