"""What identifies a device across sources (ADR 0017): serial, MAC addresses, host name.

Values are normalised so different systems agree: serials upper-cased and trimmed, with
manufacturers' placeholders left out; MACs as ``AA:BB:CC:DD:EE:FF``; host names as the
upper-case short name.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

# Serials that are a manufacturer's placeholder, not the machine's: many machines share them.
PLACEHOLDER_SERIALS = frozenset({
    "DEFAULT STRING", "SERIAL#", "TO BE FILLED BY O.E.M.", "TO BE FILLED BY OEM",
    "SYSTEM SERIAL NUMBER", "NONE", "N/A", "NA", "0", "00000000", "123456789",
    "INVALID", "NOT SPECIFIED", "NOT APPLICABLE", "CHASSIS SERIAL NUMBER",
})  # fmt: skip
_MAC = re.compile(r"[0-9A-F]{12}")
_HOST = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,62}")
# MACs that aren't one machine's: empty, broadcast, and common virtual adapters' fixed ones.
_NOT_A_MACHINE = frozenset({"000000000000", "FFFFFFFFFFFF"})


def serial(value: Any) -> str | None:
    text = " ".join(str(value or "").split()).upper()
    return None if not text or text in PLACEHOLDER_SERIALS else text


def mac(value: Any) -> str | None:
    digits = re.sub(r"[^0-9A-F]", "", str(value or "").upper())
    if not _MAC.fullmatch(digits) or digits in _NOT_A_MACHINE:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def macs(values: Any) -> list[str]:
    """MACs from a value, a list, or text with one or several (comma, space or line)."""
    items: list[Any]
    if isinstance(values, list | tuple | set):
        items = list(values)  # pyright: ignore[reportUnknownArgumentType]
    else:
        items = re.split(r"[\s,;]+", str(values or ""))
    out: list[str] = []
    for item in items:
        found = mac(item)
        if found and found not in out:
            out.append(found)
    return out


def host(value: Any) -> str | None:
    """The upper-case short host name, or None for an IP address or a description."""
    text = str(value or "").strip()
    try:
        ipaddress.ip_address(text)
        return None
    except ValueError:
        short = text.split(".")[0].upper()
        return short if _HOST.fullmatch(short) else None


def of_values(name: str, fields: dict[str, Any], *, by_name: bool) -> set[tuple[str, str]]:
    """A record's identities from its values (``by_name``: its name is a host name)."""
    found: set[tuple[str, str]] = set()
    if s := serial(fields.get("serial_number")):
        found.add(("serial", s))
    for key in ("mac_address", "mac_addresses"):
        found |= {("mac", m) for m in macs(fields.get(key))}
    for h in (host(fields.get("hostname")), host(name) if by_name else None):
        if h:
            found.add(("host", h))
    return found
