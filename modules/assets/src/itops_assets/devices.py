"""Devices: the list people review and the page they read. Pure, never AI.

The starter Device type gets its own views: a list with the facts worked out for each device
(class, in use and who saw it, age and where it comes from, which sources know it) that can
be filtered, sorted, paged and downloaded, and a record page in sections. Other asset types
keep the generic pages.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from itops_assets.classes import CLASSES

TYPE = "device"
PAGE_SIZE = 100
STATUSES = ("Active", "Spare", "Inactive", "Retired", "Unreviewed", "Ignored")
SEEN = {"network": "Seen (scan or Sophos)", "ad": "AD only", "": "Neither"}
BASIS = {
    "purchased": "purchase date",
    "warranty": "warranty end",
    "in_service": "in service since (estimate)",
    "serial": "HP serial (estimate)",
    "joined": "joined the domain (estimate)",
}
# Ready-made reviews: a name, what it shows, and the filters it sets.
VIEWS: dict[str, tuple[str, dict[str, str]]] = {
    "no_model": ("No model", {"model": "none"}),
    "unseen": ("Not seen anywhere", {"inuse": "none", "status": "not_retired"}),
    "unreviewed": ("Unreviewed", {"status": "Unreviewed"}),
    "disagree": ("Sources disagree", {"disagree": "1"}),
    "no_class": ("Computers with no class", {"cls": "none", "status": "not_retired"}),
}
SORTS = {
    "name": "Name",
    "class": "Class",
    "status": "Status",
    "age": "Age (oldest first)",
    "seen": "Last seen (longest ago first)",
}
# The record page: sections of the starter Device type's fields; anything else is "Other".
SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Identity", ("kind", "status", "computer_class", "manufacturer", "model", "serial_number",
                  "product_number", "asset_tag")),
    ("Network", ("hostname", "primary_ip", "mac_address", "default_gateway", "last_seen")),
    ("Hardware and operating system", ("operating_system", "os_notes", "processor",
                                       "memory_gb", "storage_gb", "tpm")),
    ("Lifecycle", ("purchased", "warranty_expires", "in_service", "installed_on",
                   "installed_by")),
    ("Sophos", ("sophos_last_seen", "sophos_health", "sophos_notes", "sophos_user",
                "sophos_ip")),
    ("Active Directory", ("ad_last_sign_in", "ad_account", "ad_joined", "ad_ou", "ad_status",
                          "ad_home_ou")),
    ("Where and who", ("location", "rack_position", "contact")),
    ("Notes", ("notes",)),
]  # fmt: skip


def sources_of(d: dict[str, Any]) -> list[str]:
    """The systems that sync a device now (its connectors' names, from ``fleet``)."""
    found: list[Any] = d.get("sources") or []
    return [str(x) for x in found]


def source_options(systems: list[str]) -> list[tuple[str, str]]:
    """The *Known to* filter's choices for the systems that sync devices here."""
    out = [("", "Any sources"), ("none", "Synced from none")]
    for x in systems:
        out += [(f"only:{x}", f"{x} only"), (f"not:{x}", f"Not in {x}")]
    return out


def age_years(d: dict[str, Any], today: dt.date) -> float | None:
    try:
        acquired = dt.date.fromisoformat(str(d.get("acquired"))[:10])
    except ValueError:
        return None
    return round((today - acquired).days / 365.25, 1)


@dataclass
class Filters:
    q: str = ""
    status: str = ""  # a status, "none", "not_retired", or "" (any)
    cls: str = ""  # a class, "none", or ""
    kind: str = ""
    inuse: str = ""  # network | ad | none | ""
    source: str = ""  # only:<system> | not:<system> | none | ""
    model: str = ""  # none | ""
    disagree: str = ""  # "1"
    sort: str = "name"
    page: int = 1

    @classmethod
    def from_query(cls, params: Any) -> Filters:
        def get(key: str) -> str:
            return str(params.get(key) or "").strip()

        f = cls(
            q=get("q"), status=get("status"), cls=get("cls"), kind=get("kind"),
            inuse=get("inuse"), source=get("source"), model=get("model"),
            disagree=get("disagree"),
        )  # fmt: skip
        view = VIEWS.get(get("view"))
        if view:
            for k, v in view[1].items():
                setattr(f, k, v)
        f.sort = get("sort") if get("sort") in SORTS else "name"
        f.page = int(get("page")) if get("page").isdigit() and int(get("page")) > 0 else 1
        return f

    def query(self, **changes: Any) -> dict[str, str]:
        """These filters as query parameters (blank ones left out), with ``changes``."""
        values = {k: str(v) for k, v in vars(self).items() if v not in ("", None)}
        values.pop("page", None)
        if self.sort == "name":
            values.pop("sort", None)
        values |= {k: str(v) for k, v in changes.items()}
        return {k: v for k, v in values.items() if v}


def matches(d: dict[str, Any], f: Filters) -> bool:
    status = str(d.get("status") or "")
    if f.status == "none" and status:
        return False
    if f.status == "not_retired" and status in ("Retired", "Ignored"):
        return False
    if f.status not in ("", "none", "not_retired") and status != f.status:
        return False
    cls_ = d.get("computer_class") or ""
    if f.cls == "none" and (cls_ or d.get("kind") not in ("Laptop", "Workstation")):
        return False
    if f.cls in CLASSES and cls_ != f.cls:
        return False
    if f.kind and d.get("kind") != f.kind:
        return False
    if f.inuse and (d.get("seen_by") or "none") != f.inuse:
        return False
    sources = sources_of(d)
    rule, _, system = f.source.partition(":")
    if rule == "only" and sources != [system]:
        return False
    if rule == "not" and system in sources:
        return False
    if f.source == "none" and sources:
        return False
    if f.model == "none" and d.get("model"):
        return False
    if f.disagree and not d.get("disagreements"):
        return False
    if f.q:
        keys = (
            "name",
            "model",
            "serial_number",
            "manufacturer",
            "operating_system",
            "hostname",
            "primary_ip",
        )
        hay = " ".join(str(d.get(k) or "") for k in keys)
        if f.q.lower() not in hay.lower():
            return False
    return True


def sort(devices: list[dict[str, Any]], how: str, today: dt.date) -> list[dict[str, Any]]:
    by_name = sorted(devices, key=lambda d: str(d["name"]).lower())
    if how == "class":
        return sorted(by_name, key=lambda d: (d.get("computer_class") is None,
                                              str(d.get("computer_class") or "")))  # fmt: skip
    if how == "status":
        return sorted(by_name, key=lambda d: str(d.get("status") or "~"))
    if how == "age":
        return sorted(by_name, key=lambda d: -(age_years(d, today) or -1))
    if how == "seen":
        return sorted(by_name, key=lambda d: str(d.get("seen_on") or d.get("last_seen") or ""))
    return by_name


def seen_text(d: dict[str, Any]) -> str:
    if d.get("seen_by") == "network":
        via = "Sophos" if d.get("seen_via") == "Sophos" else "Scan"
        return f"{via} {d.get('seen_on') or d.get('last_seen')}"
    if d.get("seen_by") == "ad":
        return f"AD only ({d.get('ad_last_sign_in')})"
    seen = d.get("seen_on") or d.get("last_seen")
    last = max((str(x) for x in (seen, d.get("ad_last_sign_in")) if x), default="")
    return f"No (last {last})" if last else "Never"


def age_text(d: dict[str, Any], today: dt.date) -> str:
    years = age_years(d, today)
    if years is None:
        return "Unknown"
    basis = BASIS.get(str(d.get("age_basis") or ""), "")
    return f"{years:g} y" + (f" ({basis})" if basis else "")


def row(d: dict[str, Any], today: dt.date) -> dict[str, Any]:
    """One device as a list row (and an export row)."""
    cls_ = d.get("computer_class") or ""
    return {
        "name": {"text": d["name"], "href": d["url"]},
        "class": cls_ + (" *" if cls_ and d.get("class_basis") == "set" else ""),
        "status": d.get("status") or "",
        "seen": seen_text(d),
        "age": age_text(d, today),
        "model": d.get("model") or "",
        "sources": ", ".join(sources_of(d)) or "None",
        "flag": {"badge": "Disagree", "tone": "warning"} if d.get("disagreements") else "",
    }


def export_row(d: dict[str, Any], today: dt.date) -> dict[str, Any]:
    return {
        "name": d["name"],
        "kind": d.get("kind") or "",
        "class": d.get("computer_class") or "",
        "class_basis": d.get("class_basis") or "",
        "status": d.get("status") or "",
        "seen_by": SEEN.get(str(d.get("seen_by") or ""), ""),
        "last_seen": d.get("last_seen"),
        "sophos_last_seen": d.get("sophos_last_seen"),
        "sophos_health": d.get("sophos_health") or "",
        "ad_last_sign_in": d.get("ad_last_sign_in"),
        "ad_account": d.get("ad_account") or "",
        "age": age_years(d, today),
        "age_basis": BASIS.get(str(d.get("age_basis") or ""), ""),
        "acquired": d.get("acquired"),
        "manufacturer": d.get("manufacturer") or "",
        "model": d.get("model") or "",
        "serial": d.get("serial_number") or "",
        "operating_system": d.get("operating_system") or "",
        "sources": ", ".join(sources_of(d)),
        "disagreements": " ".join(d.get("disagreements") or []),
    }
