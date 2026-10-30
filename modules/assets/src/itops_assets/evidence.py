"""Is a device in use, who says so, and where do the sources disagree? Pure, never AI.

The device itself reporting (an inventory scan's *Last seen on the network*, the Sophos agent's
*Last seen (Sophos)*; later the device agent, ADR 0023) is trusted over Active Directory (ADR
0017, amended 2026-10-20 and 2026-10-23). The latest of those is when it was last seen. AD's sign-in
counts only when no scan saw the device. Where they don't line up, or a person's status says
otherwise, each disagreement is a finding: what doesn't line up and what to do about it
(AGENTS.md, writing warnings). A person can accept one as expected; the acceptance holds
while the finding's ``facts`` stay the same.

Coverage too: a Windows PC in use that Sophos doesn't know or has stopped hearing from, Sophos
saying its protection is unhealthy, a PC Sophos sees that AD doesn't, and two machines sharing
one name. The sources checking each other keep each of them clean.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from itops_assets import identity as ids

IN_USE_DAYS = 90  # ADR 0017: in use = seen within 90 days
NOT_IN_USE = ("Retired", "Inactive", "Ignored")
GONE = ("Retired", "Ignored")  # out of service: not one of the records holding a machine
SAME_SERIAL = "same_serial"
SAME_NAME = "same_name"
ACROSS_DEVICES = (SAME_SERIAL, SAME_NAME)  # judged over the fleet, not one device's values
SOPHOS_DAYS = 14  # Sophos reports every few minutes: two weeks' silence means it stopped
# Computers people use: endpoint protection (Sophos) is expected on them. Servers aren't held
# to it: they may be protected another way.
PC_KINDS = ("Workstation", "Laptop")
COMPUTER_KINDS = (*PC_KINDS, "Server", "Virtual machine")
UNHEALTHY = ("Bad", "Suspicious")


@dataclass(frozen=True)
class Finding:
    code: str  # which disagreement ("status_vs_scan", ...): what an acceptance names
    text: str  # what doesn't line up and what to do
    facts: str  # the state it was judged on: an acceptance holds while these stay the same


@dataclass
class Evidence:
    seen_by: str  # "network" (a scan or Sophos) | "ad" (AD sign-in only) | "" (neither)
    findings: list[Finding] = field(default_factory=list[Finding])
    seen_on: str = ""  # ISO date the device itself was last seen (scan or Sophos), or ""
    seen_via: str = ""  # "a scan" | "Sophos": which said so

    @property
    def disagreements(self) -> list[str]:
        return [f.text for f in self.findings]


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def last_seen(values: dict[str, Any]) -> tuple[dt.date | None, str]:
    """When the device itself was last seen, and by what: the later of an inventory scan
    (*Last seen on the network*) and the Sophos agent (*Last seen (Sophos)*)."""
    found = [
        (d, via)
        for d, via in (
            (_date(values.get("last_seen")), "a scan"),
            (_date(values.get("sophos_last_seen")), "Sophos"),
        )
        if d
    ]
    return max(found, key=lambda x: x[0]) if found else (None, "")


def judge(
    values: dict[str, Any],
    today: dt.date,
    days: int = IN_USE_DAYS,
    *,
    ad_synced: bool = False,
    sophos_synced: bool = False,
) -> Evidence:
    """What the sources say about one device (its record's field values). ``ad_synced``: AD
    fills Assets (some device has an AD account), so a PC without one isn't in AD.
    ``sophos_synced``: likewise Sophos (some device has a Sophos last seen), so a PC without
    one isn't in Sophos; before Sophos first syncs, every PC would look unprotected."""
    since = today - dt.timedelta(days=days)
    status = str(values.get("status") or "")
    seen, via = last_seen(values)
    signed_in = _date(values.get("ad_last_sign_in"))
    ad_off = values.get("ad_account") == "Disabled"
    scanned = seen is not None and seen >= since
    by_ad = signed_in is not None and signed_in >= since and not ad_off
    out = Evidence("network" if scanned else "ad" if by_ad else "",
                   seen_on=seen.isoformat() if seen else "", seen_via=via)  # fmt: skip
    facts = f"{status}|{values.get('ad_account') or ''}"

    def say(code: str, text: str) -> None:
        out.findings.append(Finding(code, text, facts))

    if status in NOT_IN_USE:
        if scanned:
            say("status_vs_scan",
                f"Marked {status}, but {via} saw it on {seen}. If it's in use, set its "
                "status to Active. If it isn't, another machine may now have its name or "
                f"address: check what {via} reports below.")  # fmt: skip
        elif by_ad:
            say("status_vs_ad",
                f"Marked {status}, but it signed in to AD on {signed_in}. If it's in use, set "
                "its status to Active. If it isn't, something still signs in with its AD "
                "account: disable the account in AD.")  # fmt: skip
        elif status == "Retired" and values.get("ad_account") == "Enabled":
            say("retired_ad_enabled",
                "Marked Retired, but its AD account is still enabled: disable it in AD (or "
                "move it to the retired computers' OU), so nothing signs in with it.")  # fmt: skip
        return out
    if scanned:
        if ad_off:
            say("scan_vs_ad_disabled",
                f"Seen by {via} on {seen}, but its AD account is disabled. If it's in "
                "use, enable the account in AD, or it can't sign in to the domain. If it "
                "isn't, set its status to Retired.")  # fmt: skip
        elif signed_in is not None and signed_in < since:
            say("scan_vs_ad_stale",
                f"Seen by {via} on {seen}, but not signed in to AD since {signed_in}: it "
                "may have dropped off the domain, or be used with a local account. Check it's "
                "joined (rejoin it if not), or accept this if it's meant to be off the "
                "domain.")  # fmt: skip
    elif by_ad:
        say("ad_no_scan",
            f"Signed in to AD on {signed_in}, but neither a scan nor Sophos has seen it"
            + (f" since {seen}" if seen else "")
            + ". It's counted as in use on AD's word. Check why the inventory can't reach it "
            "and Sophos isn't reporting (off the network, a firewall, Sophos not installed); "
            "if the machine is gone, set its status to Retired.")  # fmt: skip
    elif status == "Active" and (seen or signed_in):
        last = max(d for d in (seen, signed_in) if d)
        say("active_not_seen",
            f"Marked Active, but nothing has seen it since {last}. If it's gone, set its "
            "status to Retired (or Inactive if it's in storage). If it's in use, check why "
            "neither a scan, Sophos nor AD sees it.")  # fmt: skip
    if status != "Spare" and not any(f.code == "ad_no_scan" for f in out.findings):
        _sophos(values, today, days, say, ad_synced=ad_synced, sophos_synced=sophos_synced)
    return out


def _sophos(values: dict[str, Any], today: dt.date, days: int, say: Callable[[str, str], None],
            *, ad_synced: bool, sophos_synced: bool) -> None:  # fmt: skip
    """Is a machine in use covered by Sophos, and is Sophos happy with it? (A machine AD
    alone sees is already ``ad_no_scan``, which says to check Sophos.)"""
    recent = today - dt.timedelta(days=SOPHOS_DAYS)
    sophos = _date(values.get("sophos_last_seen"))
    kind = str(values.get("kind") or "")
    pc = kind in PC_KINDS and "windows" in str(values.get("operating_system") or "").lower()
    scanned = _date(values.get("last_seen"))
    signed_in = _date(values.get("ad_last_sign_in"))
    if values.get("ad_account") == "Disabled":
        signed_in = None
    other = max(
        ((d, how) for d, how in ((scanned, "Seen by a scan"), (signed_in, "Signed in to AD"))
         if d), default=None)  # fmt: skip
    if pc and other and other[0] >= recent:
        when, how = other
        if sophos is None and sophos_synced:
            say("sophos_missing",
                f"{how} on {when}, but Sophos has no record of it: it isn't protected, or its "
                "agent never registered. Install Sophos on it, or check why the agent didn't "
                "register. If it's meant to run without Sophos, accept this.")  # fmt: skip
        elif sophos is not None and sophos < recent and when >= sophos + dt.timedelta(days=7):
            say("sophos_silent",
                f"Sophos last heard from it on {sophos}, but {how[0].lower() + how[1:]} on "
                f"{when}: its Sophos agent has stopped reporting. Check the agent on the "
                "machine, and reinstall it if it's broken.")  # fmt: skip
    health = str(values.get("sophos_health") or "")
    if sophos and sophos >= recent and health in UNHEALTHY:
        notes = str(values.get("sophos_notes") or "")
        say("sophos_unhealthy",
            f"Sophos reported its protection as {health} on {sophos}"
            + (f": {notes[:1].lower() + notes[1:]}" if notes else "")
            + ". Check it in Sophos Central and fix what it lists there.")  # fmt: skip
    if (ad_synced and kind in PC_KINDS and not values.get("ad_account") and sophos
            and sophos >= today - dt.timedelta(days=days)):  # fmt: skip
        say("sophos_not_in_ad",
            f"Sophos saw it on {sophos}, but AD has no computer account by its name: it "
            "isn't joined to the domain, or it's joined under another name. Join it or rename "
            "it to match. If it's meant to be off the domain, accept this.")  # fmt: skip


def same_serial(devices: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{device id: names of the other devices with its serial}, among devices not retired:
    one machine held twice."""
    by_serial: dict[str, list[dict[str, Any]]] = {}
    for d in devices:
        serial = ids.serial(d.get("serial_number"))  # placeholders ("Default string") dropped
        if serial and str(d.get("status") or "") not in GONE:
            by_serial.setdefault(serial, []).append(d)
    out: dict[str, list[str]] = {}
    for group in by_serial.values():
        if len(group) > 1:
            for d in group:
                out[str(d["id"])] = sorted(str(o["name"]) for o in group if o is not d)
    return out


def twin(others: list[str]) -> Finding:
    """The finding for a device whose serial other records also have (``same_serial``)."""
    return Finding(
        SAME_SERIAL,
        f"Another record has the same serial ({', '.join(others)}): probably one machine held "
        "twice. Merge the stale record into the other from its page (Merge into another "
        "record). If they really are different machines, accept this.",
        "|".join(sorted(others)),
    )


def same_name(devices: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{device id: the other computers with its name but another serial}, among those not
    retired: two machines sharing one name (AD has one computer account per name)."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for d in devices:
        if (str(d.get("kind") or "") in COMPUTER_KINDS and str(d.get("status") or "") not in GONE
                and ids.serial(d.get("serial_number"))):  # fmt: skip
            by_name.setdefault(str(d["name"]).upper(), []).append(d)
    out: dict[str, list[str]] = {}
    for group in by_name.values():
        if len({ids.serial(d.get("serial_number")) for d in group}) > 1:
            for d in group:
                mine = ids.serial(d.get("serial_number"))
                others = [o for o in group if ids.serial(o.get("serial_number")) != mine]
                out[str(d["id"])] = sorted(
                    f"{o['name']} (serial {ids.serial(o.get('serial_number'))})" for o in others
                )
    return out


def namesake(others: list[str]) -> Finding:
    """The finding for a computer whose name another machine also has (``same_name``)."""
    return Finding(
        SAME_NAME,
        f"Another machine has the same name but a different serial ({', '.join(others)}): two "
        "machines with one name. AD keeps one computer account per name, so only one of them "
        "can use it. Rename one, or if one is gone, set its status to Retired.",
        "|".join(sorted(others)),
    )
