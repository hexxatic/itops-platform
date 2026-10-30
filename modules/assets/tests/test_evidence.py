"""In use, who says so, and where the sources disagree (a scan is trusted over AD); each
disagreement says what to do."""

from __future__ import annotations

import datetime as dt
from typing import Any

from itops_assets.evidence import judge, namesake, same_name, same_serial, twin

TODAY = dt.date(2026, 10, 20)
RECENT, OLD = "2026-10-12", "2025-03-01"


def said(**values: Any) -> tuple[str, list[str]]:
    e = judge(values, TODAY, sophos_synced=True)
    return e.seen_by, [f.code for f in e.findings]


def text(**values: Any) -> str:
    [finding] = judge(values, TODAY, sophos_synced=True).findings
    return finding.text


def test_a_scan_and_ad_agreeing_say_nothing() -> None:
    assert said(last_seen=RECENT, ad_last_sign_in=RECENT, ad_account="Enabled") == ("network", [])
    assert said(last_seen=RECENT) == ("network", [])  # no AD record: nothing to disagree with
    assert said(last_seen=OLD, ad_last_sign_in=OLD) == ("", [])  # both say it's gone


def test_a_scan_is_trusted_over_ad() -> None:
    assert said(last_seen=RECENT, ad_last_sign_in=OLD) == ("network", ["scan_vs_ad_stale"])
    assert text(last_seen=RECENT, ad_last_sign_in=OLD).startswith(
        "Seen by a scan on 2026-10-12, but not signed in to AD since 2025-03-01"
    )
    disabled = {"last_seen": RECENT, "ad_last_sign_in": RECENT, "ad_account": "Disabled"}
    assert said(**disabled) == ("network", ["scan_vs_ad_disabled"])
    assert "enable the account in AD" in text(**disabled)


def test_sophos_counts_as_seen_like_a_scan() -> None:
    # A laptop that never comes to the office: no scan reaches it, but its Sophos agent reports.
    assert said(sophos_last_seen=RECENT, ad_last_sign_in=OLD) == ("network", ["scan_vs_ad_stale"])
    e = judge({"last_seen": OLD, "sophos_last_seen": RECENT}, TODAY)
    assert (e.seen_by, e.seen_on, e.seen_via) == ("network", RECENT, "Sophos")
    e = judge({"last_seen": RECENT, "sophos_last_seen": OLD}, TODAY)
    assert (e.seen_on, e.seen_via) == (RECENT, "a scan")  # the later of the two
    assert text(status="Retired", sophos_last_seen=RECENT).startswith(
        "Marked Retired, but Sophos saw it on 2026-10-12."
    )
    assert judge({}, TODAY).seen_on == ""


def test_ad_counts_only_when_no_scan_saw_it() -> None:
    assert said(ad_last_sign_in=RECENT) == ("ad", ["ad_no_scan"])
    assert text(ad_last_sign_in=RECENT).startswith(
        "Signed in to AD on 2026-10-12, but neither a scan nor Sophos has seen it. It's counted "
        "as in use"
    )
    assert "nor Sophos has seen it since 2025-03-01." in text(last_seen=OLD, ad_last_sign_in=RECENT)
    assert said(ad_last_sign_in=RECENT, ad_account="Disabled")[0] == ""


def test_a_status_that_doesnt_fit() -> None:
    assert said(status="Retired", last_seen=RECENT)[1] == ["status_vs_scan"]
    assert "set its status to Active" in text(status="Retired", last_seen=RECENT)
    assert said(status="Active", last_seen=OLD, ad_last_sign_in=OLD)[1] == ["active_not_seen"]
    assert "set its status to Retired" in text(status="Active", last_seen=OLD)
    assert said(status="Retired", last_seen=OLD) == ("", [])
    retired = {"status": "Retired", "ad_last_sign_in": OLD, "ad_account": "Enabled"}
    assert said(**retired)[1] == ["retired_ad_enabled"]
    assert "disable it in AD" in text(**retired)
    assert said(status="Retired", ad_last_sign_in=OLD, ad_account="Disabled") == ("", [])


def test_an_acceptance_holds_while_the_facts_do() -> None:
    # What an acceptance is matched on: the status and AD account, not the dates.
    a = judge({"last_seen": RECENT, "ad_last_sign_in": OLD, "ad_account": "Enabled"}, TODAY)
    b = judge({"last_seen": "2026-10-19", "ad_last_sign_in": OLD, "ad_account": "Enabled"}, TODAY)
    c = judge({"last_seen": RECENT, "ad_last_sign_in": OLD, "ad_account": "Disabled"}, TODAY)
    assert a.findings[0].facts == b.findings[0].facts
    assert (c.findings[0].code, c.findings[0].facts) != (a.findings[0].code, a.findings[0].facts)


def test_one_machine_held_twice() -> None:
    devices = [
        {"id": "1", "name": "HOST01", "serial_number": "abc", "status": "Active"},
        {"id": "2", "name": "HOST01", "serial_number": "ABC ", "status": None},
        {"id": "3", "name": "HOST02", "serial_number": "ABC", "status": "Retired"},
        {"id": "4", "name": "HOST03", "serial_number": "XYZ", "status": None},
        {"id": "5", "name": "HOST04", "serial_number": "Default string", "status": None},
        {"id": "6", "name": "HOST05", "serial_number": "Default string", "status": None},
    ]
    assert same_serial(devices) == {"1": ["HOST01"], "2": ["HOST01"]}
    assert "Merge the stale record into the other" in twin(["HOST01"]).text


PC = {"kind": "Laptop", "operating_system": "Windows 11 Pro", "status": "Active"}
RECENTLY = "2026-10-15"  # within Sophos's two weeks of TODAY
MONTH_AGO = "2026-09-20"


def test_a_pc_in_use_without_sophos() -> None:
    assert said(**PC, last_seen=RECENTLY)[1] == ["sophos_missing"]
    assert text(**PC, last_seen=RECENTLY).startswith(
        "Seen by a scan on 2026-10-15, but Sophos has no record of it"
    )
    signed_in = {**PC, "ad_last_sign_in": RECENTLY, "ad_account": "Enabled", "last_seen": MONTH_AGO}
    assert said(**signed_in)[1] == ["sophos_missing"]
    assert text(**signed_in).startswith("Signed in to AD on 2026-10-15, but Sophos")
    # No status yet counts as in use; Spare (on the shelf) and Retired don't.
    assert said(**(PC | {"status": None}), last_seen=RECENTLY)[1] == ["sophos_missing"]
    assert said(**(PC | {"status": "Spare"}), last_seen=RECENTLY) == ("network", [])
    # Servers aren't held to it, nor anything that isn't Windows.
    assert said(**(PC | {"kind": "Server"}), last_seen=RECENTLY) == ("network", [])
    assert said(**(PC | {"operating_system": "macOS"}), last_seen=RECENTLY) == ("network", [])
    # Seen only longer ago than two weeks: nothing to say yet.
    assert said(**PC, last_seen=MONTH_AGO) == ("network", [])
    # AD alone sees it: ad_no_scan already says to check Sophos, once.
    assert said(**PC, ad_last_sign_in=RECENTLY, ad_account="Enabled")[1] == ["ad_no_scan"]
    # Before Sophos first syncs (no device has a Sophos last seen), every PC lacks one.
    assert judge({**PC, "last_seen": RECENTLY}, TODAY).findings == []


def test_sophos_stopped_reporting() -> None:
    silent = {**PC, "sophos_last_seen": MONTH_AGO, "last_seen": RECENTLY}
    assert said(**silent)[1] == ["sophos_silent"]
    assert text(**silent).startswith(
        "Sophos last heard from it on 2026-09-20, but seen by a scan on 2026-10-15"
    )
    # Seen by a scan the same week Sophos last reported: just switched off, not broken.
    assert said(**PC, sophos_last_seen="2026-10-03", last_seen="2026-10-06") == ("network", [])
    assert said(**PC, sophos_last_seen=RECENTLY, last_seen=RECENTLY) == ("network", [])


def test_sophos_unhealthy() -> None:
    bad = {"sophos_last_seen": RECENTLY, "sophos_health": "Bad",
           "sophos_notes": "Services not running (Sophos File Scanner)"}  # fmt: skip
    assert said(**bad)[1] == ["sophos_unhealthy"]
    assert text(**bad) == (
        "Sophos reported its protection as Bad on 2026-10-15: services not running (Sophos "
        "File Scanner). Check it in Sophos Central and fix what it lists there."
    )
    assert said(**(bad | {"sophos_health": "Good"}))[1] == []
    assert said(**(bad | {"sophos_last_seen": MONTH_AGO}))[1] == []  # old news: silent instead


def test_a_pc_sophos_sees_that_ad_doesnt() -> None:
    seen = {**PC, "sophos_last_seen": RECENTLY}
    assert judge(seen, TODAY).findings == []  # no AD in Assets: nothing to compare with
    [finding] = judge(seen, TODAY, ad_synced=True).findings
    assert finding.code == "sophos_not_in_ad" and "AD has no computer account" in finding.text
    assert judge(seen | {"ad_account": "Enabled"}, TODAY, ad_synced=True).findings == []
    other = seen | {"kind": "Other"}  # Zoom rooms, cloud PCs, Macs: not expected in AD
    assert judge(other, TODAY, ad_synced=True).findings == []


def test_two_machines_with_one_name() -> None:
    devices = [
        {"id": "1", "name": "HOST01", "kind": "Laptop", "serial_number": "AAA", "status": "Active"},
        {"id": "2", "name": "host01", "kind": "Laptop", "serial_number": "BBB", "status": None},
        {"id": "3", "name": "HOST01", "kind": "Laptop", "serial_number": "CCC",
         "status": "Retired"},
        {"id": "4", "name": "HOST02", "kind": "Server", "serial_number": "DDD", "status": None},
        {"id": "5", "name": "HOST02", "kind": "Server", "serial_number": "ddd", "status": None},
        {"id": "6", "name": "PRN01", "kind": "Printer", "serial_number": "EEE", "status": None},
        {"id": "7", "name": "PRN01", "kind": "Printer", "serial_number": "FFF", "status": None},
        {"id": "8", "name": "HOST03", "kind": "Laptop", "serial_number": "", "status": None},
        {"id": "9", "name": "HOST03", "kind": "Laptop", "serial_number": "GGG", "status": None},
    ]  # fmt: skip
    assert same_name(devices) == {
        "1": ["host01 (serial BBB)"], "2": ["HOST01 (serial AAA)"],
    }  # fmt: skip
    assert "Rename one" in namesake(["host01 (serial BBB)"]).text
