"""When a computer was built, from what's written on it: for an age when no purchase or
warranty date is known. Pure functions, never AI.

HP's serial numbers carry the build week: ``MXL0042...`` is 2020 (the 4th character is the
year's last digit), week 04 (the 5th and 6th). The decade is the latest one that isn't in the
future, unless the processor came out too long before it (a Core i3-6100 from 2015 in a
computer "built" in 2026 was built in 2016). Other makers' serials carry no date.
"""

from __future__ import annotations

import datetime as dt
import re

SERIAL_MAKERS = ("HP", "HEWLETT")  # Manufacturer starts with one of these
_HP_SERIAL = re.compile(r"[A-Z0-9]{3}(\d)(\d\d)[A-Z0-9]{4}")
# A computer isn't built with a processor older than this.
NEWEST_AFTER_PROCESSOR = 5

_CORE = re.compile(r"\bcore\s*(?:\(tm\))?\s*i[3579]-(\d{4,5})", re.I)
_ULTRA = re.compile(r"\bcore\s*(?:\(tm\))?\s*ultra\s+[3579]\s+(\d)\d\d", re.I)
_RYZEN = re.compile(r"\bryzen\s*(?:\(tm\))?\s+[3579]\s+(?:pro\s+)?(\d)\d\d\d", re.I)
# Intel Core generation -> the year its first processors came out.
_CORE_YEARS = {2: 2011, 3: 2012, 4: 2013, 5: 2015, 6: 2015, 7: 2016, 8: 2017, 9: 2018,
               10: 2019, 11: 2020, 12: 2021, 13: 2022, 14: 2023}  # fmt: skip
_ULTRA_YEARS = {1: 2023, 2: 2024, 3: 2025}


def processor_year(processor: str | None) -> int | None:
    """The year a processor's generation came out (Intel Core, Core Ultra, AMD Ryzen), or None."""
    text = processor or ""
    if m := _CORE.search(text):
        digits = m[1]
        # i5-14500T is generation 14; i5-1135G7 (a laptop's four digits starting 1) is 11.
        gen = int(digits[:2]) if len(digits) == 5 or digits[0] == "1" else int(digits[0])
        return _CORE_YEARS.get(gen)
    if m := _ULTRA.search(text):
        return _ULTRA_YEARS.get(int(m[1]))
    if m := _RYZEN.search(text):
        series = int(m[1])
        return 2016 + series if series <= 8 else 2024
    return None


def _monday(year: int, week: int) -> dt.date:
    try:
        return dt.date.fromisocalendar(year, week, 1)
    except ValueError:  # week 53 in a year with 52
        return dt.date.fromisocalendar(year, 52, 1)


def built_from_serial(
    manufacturer: str | None, serial: str | None, processor: str | None, today: dt.date
) -> dt.date | None:
    """The Monday of the week an HP computer was built, from its serial number, or None
    (another maker, or a serial that doesn't carry a date)."""
    if not (manufacturer or "").strip().upper().startswith(SERIAL_MAKERS):
        return None
    m = _HP_SERIAL.fullmatch((serial or "").strip().upper())
    if not m or not 1 <= int(m[2]) <= 53:
        return None
    digit, week = int(m[1]), int(m[2])
    year = today.year - (today.year - digit) % 10
    if _monday(year, week) > today:
        year -= 10
    launched = processor_year(processor)
    if launched is not None and year - launched > NEWEST_AFTER_PROCESSOR:
        year -= 10
    return _monday(year, week)
