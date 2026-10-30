"""OS readiness (ADR 0019): can a device's hardware run an operating system it could move to?

Pure functions, never AI. A *target* is an operating system with its hardware requirements
(processors, memory, storage, TPM) and the systems it replaces. Admins keep the targets
(``assets.os_targets``); presets here are a starting point, so the next Windows is a new
target, not new code. A device is checked against every target that replaces the system it
runs, from the hardware fields on its record (``processor``, ``memory_gb``, ``storage_gb``,
``tpm``), and comes out ready, not ready or unknown, with the reasons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

READY, NOT_READY, UNKNOWN = "ready", "not_ready", "unknown"
STATUS_LABELS = {READY: "Ready", NOT_READY: "Not ready", UNKNOWN: "Unknown"}
STATUS_TONES = {READY: "success", NOT_READY: "danger", UNKNOWN: "warning"}

# The record fields readiness reads (the starter Device type has them).
HARDWARE_FIELDS = ("processor", "memory_gb", "storage_gb", "tpm")

_MARKS = re.compile(r"\((?:R|TM|C)\)|[®™]", re.I)
_SPEED = re.compile(r"@\s*[\d.]+\s*[GM]Hz", re.I)


@dataclass
class Target:
    name: str
    upgrades_from: list[str] = field(default_factory=list[str])
    processors: list[str] = field(default_factory=list[str])  # patterns; any one matches
    min_memory_gb: float | None = None
    min_storage_gb: float | None = None
    min_tpm: str | None = None  # "2.0"
    id: int | None = None


@dataclass
class Readiness:
    target: str
    status: str  # ready | not_ready | unknown
    why: list[str] = field(default_factory=list[str])

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "status": self.status, "why": list(self.why)}


def clean_processor(name: str) -> str:
    """A processor name without (R)/(TM) marks, "CPU", the clock speed and doubled spaces:
    "Intel(R) Core(TM) i5-6500 CPU @ 3.20GHz" -> "Intel Core i5-6500"."""
    name = _SPEED.sub("", _MARKS.sub(" ", name))
    return " ".join(w for w in name.split() if w.upper() != "CPU")


def pattern_lines(text: str) -> list[str]:
    """Processor patterns as typed: one per line, ``#`` starts a comment."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def bad_patterns(patterns: list[str]) -> list[str]:
    """The patterns that aren't valid regular expressions."""
    bad: list[str] = []
    for p in patterns:
        try:
            re.compile(p)
        except re.error:
            bad.append(p)
    return bad


def _os_pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(name.strip()) + r"\b", re.I)


def applies(target: Target, os_name: str) -> bool:
    """Whether a device running ``os_name`` would move to ``target``."""
    return bool(os_name) and any(
        _os_pattern(n).search(os_name) for n in target.upgrades_from if n.strip()
    )


def _number(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _version(text: str) -> float | None:
    m = re.match(r"\s*(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _gb(n: float) -> str:
    return f"{n:g} GB"


def check(target: Target, hardware: dict[str, Any]) -> Readiness:
    """``hardware``: a device's values (``processor``, ``memory_gb``, ``storage_gb``,
    ``tpm``). Not ready if any requirement fails; unknown if none fails but one can't be
    checked; ready otherwise."""
    failed: list[str] = []
    missing: list[str] = []
    processor = clean_processor(str(hardware.get("processor") or ""))
    if target.processors:
        if not processor:
            missing.append("No processor recorded (an inventory sync with hardware fills it in)")
        elif not any(re.search(p, processor, re.I) for p in target.processors):
            failed.append(f"{processor} isn't on {target.name}'s processor list")
    if target.min_memory_gb:
        memory = _number(hardware.get("memory_gb"))
        if memory is None:
            missing.append("No memory size recorded (an inventory sync with hardware fills it in)")
        elif memory < target.min_memory_gb:
            failed.append(f"{_gb(memory)} memory, needs {_gb(target.min_memory_gb)}")
    if target.min_storage_gb:
        storage = _number(hardware.get("storage_gb"))
        if storage is None:
            missing.append("No storage size recorded (an inventory sync with hardware fills it in)")
        elif storage < target.min_storage_gb:
            failed.append(f"{_gb(storage)} storage, needs {_gb(target.min_storage_gb)}")
    needed = _version(target.min_tpm or "")
    if needed:
        tpm = str(hardware.get("tpm") or "").strip()
        version = _version(tpm)
        if not tpm:
            missing.append("No TPM recorded (an inventory sync with hardware fills it in)")
        elif tpm.lower() == "none":
            failed.append(f"No TPM, needs TPM {target.min_tpm}")
        elif version is None or version < needed:
            failed.append(f"TPM {tpm}, needs {target.min_tpm}")
    if failed:
        return Readiness(target.name, NOT_READY, failed + missing)
    if missing:
        return Readiness(target.name, UNKNOWN, missing)
    return Readiness(target.name, READY)


def readiness(targets: list[Target], values: dict[str, Any]) -> list[Readiness]:
    """A device's readiness for every target that replaces the system it runs."""
    os_name = str(values.get("operating_system") or "")
    return [check(t, values) for t in targets if applies(t, os_name)]


# ---- presets ------------------------------------------------------------------------------

# Microsoft's Windows 11 requirements: "Windows 11 supported Intel/AMD/Qualcomm processors"
# (learn.microsoft.com, updated 2025-10), 4 GB memory, 64 GB storage, TPM 2.0. UEFI with
# Secure Boot is required too, but no inventory source reports it, so it isn't checked.
# Later generations meeting the same rules count as supported, so the patterns are open-ended.
WINDOWS_11_PROCESSORS = """\
# Intel Core 8th generation and later (i3-8100, i5-10210U, i7-1185G7, i9-13900K)
Intel Core i[3579]-(?:[89]\\d{3}|1\\d{3,4})
Intel Core m3-8
Intel Core (?:Ultra )?[3579] \\d{3}   # Core 3/5/7, Core Ultra (Series 1 and later)
Intel Core i[579]-[79]\\d{3}X     # Core X series 7000X/9000X
Intel Core i9-10\\d{3}X
# Intel Xeon
Intel Xeon (?:Bronze|Silver|Gold|Platinum)   # Scalable, every generation
Intel Xeon W-(?:1[0-3]\\d\\d|2[12]\\d\\d|3[13]\\d\\d|1[01]\\d{3}M)
Intel Xeon E-2[1-4]\\d\\d
Intel Xeon D-(?:17|18|27|28)\\d\\d
# Intel Pentium, Celeron and others
Intel Pentium (?:Gold|Silver)
Intel Pentium (?:4\\d{3}[UY]|[5-8]\\d{3}|G[57]\\d{3}|J5\\d{3}|N6\\d{3})
Intel Celeron (?:[3-7]\\d{3}|G[4-6]\\d{3}|J4\\d{3}|N[45]\\d{3})
Intel(?: Processor)? (?:N\\d{2,3}|U300|300\\b)
# AMD: Ryzen 2000 and later, Threadripper 2000 and later, EPYC 2nd generation and later
AMD Ryzen [3579](?: PRO)? [2-9]\\d{3}
AMD Ryzen (?:AI|Z1|Z2|Embedded)
AMD Ryzen Threadripper(?: PRO)? (?:[2-9]\\d{3})
AMD EPYC \\d{3}[2-9]
AMD Athlon (?:Gold|Silver|Pro)? ?(?:3\\d{3}|7\\d{3})
AMD Athlon 300
AMD 3015
# Qualcomm Snapdragon (Arm)
Snapdragon
"""

PRESETS: dict[str, dict[str, Any]] = {
    "Windows 11": {
        "upgrades_from": ["Windows 10", "Windows 8.1", "Windows 8", "Windows 7"],
        "processors": WINDOWS_11_PROCESSORS,
        "min_memory_gb": 4,
        "min_storage_gb": 64,
        "min_tpm": "2.0",
        "source": "Microsoft's Windows 11 requirements and supported processor lists (2025-10)",
        "notes": "UEFI with Secure Boot is also required; no inventory source reports it, so "
        "it isn't checked. A TPM that's turned off counts by its version.",
    },
}
