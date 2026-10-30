"""Budget (ADR 0016): the forecast's arithmetic, the pages, and who may change what."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from itops_budget import engine, module
from itops_budget import service as budget
from itops_budget.engine import Assumptions, Price, SeatRule
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing import assert_conformant, assert_migrations_roundtrip
from itops.testing.app import McpClient, ToolError, acting_as, make_app

TODAY = dt.date(2026, 10, 15)
OWNER = Principal(
    "paul@example.com",
    "Paul",
    frozenset({"budget.read", "budget.write", "budget.approve"}),
)
READER = Principal("aaron@example.com", "Aaron", frozenset({"budget.read"}))
OTHER = Principal("tech@example.com", "Tech", frozenset({"docs.read", "reports.read"}))


def device(name: str, kind: str, acquired: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "url": f"/assets/r/{name}",
        "kind": kind,
        "status": None,
        "last_seen": "2026-10-12",
        "acquired": acquired,
        **extra,
    }


def person(pay: str, hired: str, terminated: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "status": "Terminated" if terminated else "Active",
        "pay_type": pay,
        "hire_date": hired,
        "termination_date": terminated,
        **extra,
    }


PRICES = [
    Price("Standard laptop", "laptop", 1200.0),
    Price("Standard desktop", "desktop", 900.0),
    Price("Dock", "kit", 250.0),
    Price("Monitor", "kit", 150.0),
]


def test_conformance() -> None:
    assert_conformant(module)


async def test_migrations_roundtrip(database_url: str) -> None:
    await assert_migrations_roundtrip(module, database_url)


def test_the_forecast_adds_up_from_the_fleet_hiring_and_prices() -> None:
    fleet = [
        device("NEW1", "Laptop", "2025-01-10"),  # under 3: 2%
        device("MID1", "Workstation", "2022-06-01"),  # 3 to 4: 5%
        device("OLD1", "Workstation", "2017-01-01"),  # 9 years: must replace
        device("W10", "Laptop", "2024-01-01", operating_system="Windows 10 Pro"),  # must replace
        device("UNK1", "Laptop", None),  # unknown age: 10%
        device("GONE", "Laptop", "2024-01-01", last_seen="2026-01-01"),  # not seen: left out
        device("SPARE", "Laptop", "2024-01-01", status="Spare"),
        device("RET", "Laptop", "2019-01-01", status="Retired"),
        device("PRN", "Printer", "2019-01-01"),  # not a computer
    ]
    people = [
        person("Salary", "2023-03-01"),
        person("Salary", "2023-05-01"),
        person("Salary", "2023-07-01"),
        person("Salary", "2024-02-01"),
        person("Salary", "2024-03-01"),
        person("Salary", "2020-01-01", terminated="2025-04-01"),
        person("Hourly", "2025-01-01"),  # no seat of their own
        person("Hourly", "2025-02-01", dept_group="Engineering"),
    ]
    a = Assumptions(seat_rule=SeatRule(pay_types=["Salary"], dept_groups=["Engineering"]))
    f = engine.forecast(
        fleet=fleet, people=people, purchases=None, prices=PRICES, assumptions=a, today=TODAY
    )

    assert f.computers_in_use == 5 and f.by_class == {
        "standard_laptop": 3, "standard_desktop": 2, "engineering_laptop": 0,
        "engineering_desktop": 0}  # fmt: skip
    assert (f.spares, f.not_seen) == (1, 1)
    assert sorted(c.name for c in f.must_replace) == ["OLD1", "W10"]
    lines = {x.key: x for x in f.lines}
    assert (lines["must_replace"].count, lines["must_replace"].amount) == (2, 2100.0)
    # NEW1 2% x 1200 + MID1 5% x 900 + UNK1 10% x 1200 = 24 + 45 + 120
    assert lines["replacements"].amount == 189.0
    # Seats: 2023 +3, 2024 +2, 2025 +1 hired (Engineering) -1 left = 0 -> expected 1.7;
    # less the 1 spare = 0.7 seats to buy, at the blended price (3/5 laptops) plus the kit.
    assert f.net_seats == {"low": 0.0, "expected": 1.7, "high": 3.0}
    assert lines["growth"].count == pytest.approx(0.7)
    assert lines["growth"].amount == round(0.7 * (0.6 * 1200 + 0.4 * 900 + 400), 2)
    assert lines["peripherals"].amount == round(5 * 0.05 * 400, 2)
    subtotal = sum(x.amount or 0 for x in f.lines if x.key != "contingency")
    assert lines["contingency"].amount == round(subtotal * 0.1, 2)
    assert f.total == round(subtotal * 1.1, 2) and sum(f.quarters) == pytest.approx(f.total)
    low, high = f.scenarios["low"], f.scenarios["high"]
    assert low is not None and high is not None and f.total is not None
    assert low < f.total < high
    assert f.predicted_seats == 6  # 5 active salaried + the Engineering hire
    assert any("no purchase or warranty date" in g for g in f.gaps)
    assert any("not seen by a scan or Sophos, or signed in to AD" in g for g in f.gaps)


def test_ages_estimated_from_serial_numbers_are_listed() -> None:
    fleet = [
        device("HP1", "Laptop", "2024-12-16", age_basis="serial"),
        device("HP2", "Laptop", "2025-03-01", age_basis="purchased"),
        device("DELL1", "Laptop", "2021-07-27", age_basis="in_service"),
    ]
    f = engine.forecast(
        fleet=fleet, people=None, purchases=None, prices=PRICES, assumptions=Assumptions(),
        today=TODAY,
    )  # fmt: skip
    assert any(g.startswith("The age of 1 computer is estimated") for g in f.gaps)
    assert any('estimated "In service since" date' in g for g in f.gaps)


def test_each_class_is_priced_as_itself() -> None:
    fleet = [
        device("E1", "Laptop", "2016-01-01", computer_class="Engineering Laptop"),  # must go
        device("S1", "Laptop", "2016-01-01", computer_class="Standard Laptop"),  # must go
        device("D1", "Workstation", "2016-01-01"),  # no class in Assets: standard
    ]
    prices = [*PRICES, Price("ZBook", "engineering_laptop", 2500.0)]
    f = engine.forecast(fleet=fleet, people=None, purchases=None, prices=prices,
                        assumptions=Assumptions(), today=TODAY)  # fmt: skip
    must = {x.key: x for x in f.lines}["must_replace"]
    assert must.amount == 2500.0 + 1200.0 + 900.0
    assert any("1 computer in use has no class in Assets" in g for g in f.gaps)
    # No engineering price yet: priced as standard, and said so.
    f = engine.forecast(fleet=fleet, people=None, purchases=None, prices=PRICES,
                        assumptions=Assumptions(), today=TODAY)  # fmt: skip
    assert {x.key: x for x in f.lines}["must_replace"].amount == 1200.0 * 2 + 900.0
    assert any("no engineering laptop price, so 1 engineering laptop is priced as standard"
               in g for g in f.gaps)  # fmt: skip


def test_assets_decides_who_saw_a_computer() -> None:
    # Assets' verdict (seen_by, disagreements) decides; without one, a scan in the window.
    fleet = [
        device("SEEN", "Laptop", "2024-01-01", seen_by="network", disagreements=[]),
        device("ADONLY", "Laptop", "2024-01-01", last_seen=None, seen_by="ad",
               disagreements=["Signed in to AD on 2026-10-01, but nothing else has seen it."]),
        device("NOAD", "Laptop", "2024-01-01", seen_by="network",
               disagreements=["Seen on the network, but not signed in to AD since 2025-03-01."]),
        device("GONE", "Laptop", "2024-01-01", seen_by="", disagreements=[]),
        device("OLDAPI", "Laptop", "2024-01-01"),  # no verdict: its last_seen decides
    ]  # fmt: skip
    f = engine.forecast(
        fleet=fleet, people=None, purchases=None, prices=PRICES, assumptions=Assumptions(),
        today=TODAY,
    )  # fmt: skip
    assert f.computers_in_use == 4 and f.not_seen == 1
    [gap] = [g for g in f.gaps if g.startswith("Sources disagree on 2 computers counted here, "
                                               "1 of them seen only by AD")]  # fmt: skip
    assert f.gap_links[gap] == {"href": "/assets/disagreements", "text": "Sources disagree"}


def test_hardware_readiness_beats_the_model_list() -> None:
    # Assets' OS readiness (ADR 0019): ready for a supported system = upgrade; ready for none
    # = replace, with why; unknown = the model list decides.
    w10 = {"operating_system": "Windows 10 Pro"}

    def ready(status: str, *why: str) -> list[dict[str, object]]:
        return [{"target": "Windows 11", "status": status, "why": list(why)}]

    fleet = [
        device("READY", "Workstation", "2020-01-01", model="Odd Tower", os_ready=ready("ready"),
               **w10),
        device("NOPE", "Laptop", "2020-01-01", model="Good Book",
               os_ready=ready("not_ready", "Intel Core i5-6300U isn't on the list"), **w10),
        device("UNSURE", "Laptop", "2021-01-01", model="Good Book",
               os_ready=ready("unknown", "No TPM recorded"), **w10),
    ]  # fmt: skip
    a = Assumptions(upgradeable_models=["Good Book"])
    f = engine.forecast(
        fleet=fleet, people=None, purchases=None, prices=PRICES, assumptions=a, today=TODAY
    )
    assert sorted(c.name for c in f.upgrades) == ["READY", "UNSURE"]
    assert [c.name for c in f.must_replace] == ["NOPE"]
    assert f.must_replace[0].reasons == [
        "runs Windows 10 Pro; can't run Windows 11: Intel Core i5-6300U isn't on the list"
    ]
    assert {c.name: c.judged for c in f.upgrades} == {"READY": "hardware", "UNSURE": "model"}
    lines = {x.key: x for x in f.lines}
    assert lines["upgrade"].basis == "partial"
    assert any("judged by model (Good Book)" in g for g in f.gaps)
    # A target that's itself unsupported doesn't count.
    old = [device("X", "Laptop", "2021-01-01", operating_system="Windows 8.1",
                  os_ready=[{"target": "Windows 10", "status": "ready", "why": []}])]  # fmt: skip
    f = engine.forecast(
        fleet=old, people=None, purchases=None, prices=PRICES, assumptions=Assumptions(),
        today=TODAY,
    )  # fmt: skip
    assert [c.name for c in f.must_replace] == ["X"]


def test_upgradeable_windows_10_is_upgraded_and_production_pcs_are_left_out() -> None:
    w10 = {"operating_system": "Windows 10 Pro"}
    fleet = [
        device("CANT", "Workstation", "2020-01-01", model="Old Tower", **w10),
        device("CAN", "Laptop", "2021-01-01", model="Good Book", **w10),
        device("CANOLD", "Laptop", "2016-01-01", model="Good Book", **w10),  # 10 years: replace
        device("W11", "Laptop", "2021-01-01", model="Good Book", operating_system="Windows 11"),
        device("GAUGE", "Workstation", None, model="Gauge PC", **w10),
    ]
    a = Assumptions(upgradeable_models=["good book"], equipment_models=["Gauge PC"])
    f = engine.forecast(
        fleet=fleet, people=None, purchases=None, prices=PRICES, assumptions=a, today=TODAY
    )
    assert sorted(c.name for c in f.must_replace) == ["CANOLD", "CANT"]
    (cant,) = [c for c in f.must_replace if c.name == "CANT"]
    assert cant.reasons == [
        "runs Windows 10 Pro; hardware not checked, and model Old Tower isn't on the "
        "upgradeable list"
    ]
    assert [c.name for c in f.upgrades] == ["CAN"] and [c.name for c in f.left_out] == ["GAUGE"]
    assert f.computers_in_use == 4  # the production PC isn't in the fleet being budgeted
    lines = {x.key: x for x in f.lines}
    assert (lines["upgrade"].count, lines["upgrade"].amount) == (1, 0.0)
    assert lines["must_replace"].amount == 1200.0 + 900.0
    assert any("judged by model (Good Book, Old Tower)" in g for g in f.gaps)
    assert lines["upgrade"].basis == "estimate"
    # Without the lists, v1's rule: every Windows 10 computer must be replaced.
    f = engine.forecast(
        fleet=fleet,
        people=None,
        purchases=None,
        prices=PRICES,
        assumptions=Assumptions(),
        today=TODAY,
    )
    assert len(f.must_replace) == 4 and not f.upgrades and not f.left_out
    assert "upgrade" not in {x.key for x in f.lines}


def test_missing_data_never_stops_a_forecast() -> None:
    fleet = [device("A", "Laptop", "2020-01-01"), device("B", "Workstation", None)]
    f = engine.forecast(
        fleet=fleet, people=None, purchases=None, prices=[], assumptions=Assumptions(), today=TODAY
    )
    assert f.computers_in_use == 2 and f.total is None and f.quarters == []
    # A is 7 by 1 January 2027, the budget year (30%); B's age is unknown (10%).
    assert {x.key: x.count for x in f.lines}["replacements"] == pytest.approx(0.4)
    assert f.missing_prices == ["Standard laptop", "Standard desktop", "Kit for a new seat"]
    gaps = " ".join(f.gaps)
    assert "No HR headcount dataset" in gaps and "price list has no" in gaps
    assert "No purchase history" in gaps


def test_a_fleet_nobody_has_seen_says_why_nothing_counts() -> None:
    fleet = [device(f"D{i}", "Laptop", None, last_seen=None) for i in range(3)]
    f = engine.forecast(
        fleet=fleet, people=None, purchases=None, prices=[], assumptions=Assumptions(), today=TODAY
    )
    assert f.computers_in_use == 0 and f.not_seen == 3
    assert f.gaps[0].startswith("No computers count as in use: 3 computers haven't been seen")


def test_purchase_history_gives_past_spend_and_replacements() -> None:
    purchases = [
        {
            "date": "2025-11-02",
            "category": "Laptop",
            "quantity": 3,
            "total": 3600,
            "reason": "Replacement",
        },
        {
            "date": "2026-02-10",
            "category": "Monitor",
            "quantity": 4,
            "total": 600,
            "reason": "New seat",
        },
        {
            "date": "2024-05-01",
            "category": "Desktop",
            "quantity": 1,
            "unit_cost": 850,
            "reason": "Replacement",
        },
    ]
    assert engine.actual_spend(purchases) == [
        {"year": 2024, "computers": 850.0, "peripherals": 0.0, "other": 0.0},
        {"year": 2025, "computers": 3600.0, "peripherals": 0.0, "other": 0.0},
        {"year": 2026, "computers": 0.0, "peripherals": 600.0, "other": 0.0},
    ]
    assert engine.replacements_bought(purchases, TODAY) == 3


def test_a_steady_policy_shows_when_computers_come_due() -> None:
    fleet = [device(f"D{i}", "Workstation", f"{2021 + i % 2}-03-01") for i in range(10)]
    f = engine.forecast(
        fleet=fleet,
        people=[],
        purchases=None,
        prices=PRICES,
        assumptions=Assumptions(steady_years=5),
        today=TODAY,
    )
    turning = {t["year"]: t["count"] for t in f.steady["turning"]}
    assert turning[2026] == 5 and turning[2027] == 5  # bought 2021 and 2022
    assert f.steady["per_year"] == 2.0 and f.steady["yearly"] == 1800.0


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            for table in ("skips", "exclusions", "proposals", "versions", "prices", "settings"):
                await s.execute(text(f"DELETE FROM budget.{table}"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


def assumption_form(**changes: str) -> dict[str, str]:
    form = {
        "laptop_kinds": "Laptop",
        "desktop_kinds": "Workstation",
        "in_use_days": "90",
        "must_replace_age": "8",
        "unsupported_os": "Windows 7, Windows 8",
        "pay_types": "Salary",
        "dept_groups": "",
        "departments": "",
        "positions": "",
        "active_status": "Active",
        "growth_years": "3",
        "kit_replacement_pct": "5",
        "contingency_pct": "10",
        "steady_years": "5",
        "low_factor": "75",
        "high_factor": "125",
        "hr_dataset": "hr-employees",
        "purchases_dataset": "device-purchases",
    }
    form |= {f"rate_{i}": str(r) for i, r in enumerate((2, 5, 15, 30, 10))}
    return form | changes


async def test_people_with_budget_access_change_assumptions_prices_and_versions(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        with acting_as(OWNER):
            page = (await c.get("/budget/")).text
            assert "Device budget" in page and "Assets isn&#39;t available" in page
            r = await c.post("/api/budget/assumptions", data=assumption_form(contingency_pct="15"))
            assert r.headers["location"] == "/budget/"
            r = await c.post("/api/budget/assumptions", data=assumption_form(growth_years="x"))
            assert "error=" in r.headers["location"]
            r = await c.post(
                "/api/budget/prices",
                data={"item": "Standard laptop", "category": "laptop", "unit_price": "$1,250.00"},
            )
            assert "error" not in r.headers["location"]
            assert "$1,250.00" in (await c.get("/budget/assumptions")).text
            r = await c.post("/api/budget/versions", data={"title": "FY27 proposal"})
            m = re.match(r"/budget/v/(\d+)$", r.headers["location"])
            assert m, r.headers["location"]
            assert "FY27 proposal" in (await c.get(r.headers["location"])).text
            summary = (await c.get(f"/budget/v/{m[1]}/summary")).text
            assert "What this buys" in summary and "Draft for review" in summary
            await c.post(f"/api/budget/versions/{m[1]}/adopt")
        async with module.session() as s:
            a = await budget.assumptions(s)
            assert a.contingency_pct == 15 and a.growth_years == 3
            (v,) = await budget.versions(s)
            assert v.status == "adopted" and v.adopted_by == OWNER.user
        with acting_as(READER):  # sees it, changes nothing
            assert "FY27 proposal" in (await c.get("/budget/")).text
            assert (await c.get("/budget/assumptions")).status_code == 403
            r = await c.post(
                "/api/budget/prices", data={"item": "x", "category": "kit", "unit_price": "1"}
            )
            assert r.status_code == 403
            assert (await c.post(f"/api/budget/versions/{m[1]}/adopt")).status_code == 403
            summary = (await c.get(f"/budget/v/{m[1]}/summary")).text
            assert "Adopted" in summary and OWNER.user in summary
            r = await c.get(f"/api/budget/versions/{m[1]}/summary?format=csv")
            assert r.status_code == 200 and "Contingency" in r.text
        with acting_as(OTHER):  # granted by group: a plain reader gets nothing
            assert (await c.get("/budget/")).status_code == 403


async def test_a_person_leaves_a_computer_out_with_a_reason_and_it_is_listed(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = [
        device("HOST01", "Workstation", "2021-01-01", model="Tower"),
        device("HOST02", "Workstation", "2021-01-01", model="Tower"),
        device("GAUGE1", "Workstation", None, model="Gauge PC"),
    ]

    async def rows(key: str, *args: Any, **kw: Any) -> list[dict[str, Any]] | None:
        return fleet if key == "assets.fleet" else None

    monkeypatch.setattr(budget, "_service_rows", rows)
    async with running(database_url, tmp_path) as c:
        with acting_as(OWNER):
            await c.post(
                "/api/budget/assumptions", data=assumption_form(equipment_models="Gauge PC")
            )
            r = await c.post("/api/budget/exclusions", data={"name": "host01", "reason": ""})
            assert "error=" in r.headers["location"]
            r = await c.post("/api/budget/exclusions", data={"name": "NOPE", "reason": "x"})
            assert "error=" in r.headers["location"]
            r = await c.post(
                "/api/budget/exclusions", data={"name": "host01", "reason": "Runs the CMM"}
            )
            assert r.headers["location"] == "/budget/#left-out"
            page = (await c.get("/budget/")).text
            assert "Not in this budget" in page and "Runs the CMM" in page
            assert "Its own equipment (model Gauge PC)" in page and "Bring back" in page
        async with module.session() as s:
            f, _ = await budget.current(s)
            assert f.computers_in_use == 1 and {x.name for x in f.left_out} == {"GAUGE1", "HOST01"}
            (x,) = await budget.exclusions(s)
        with acting_as(Principal(OWNER.user, OWNER.display_name, OWNER.permissions, via_ai=True)):
            async with module.session() as s:
                with pytest.raises(budget.BudgetError):
                    await budget.exclude(s, module.current_principal(), "HOST02", "AI")  # type: ignore[arg-type]
        with acting_as(OWNER):
            r = await c.post("/api/budget/versions", data={"title": "With exclusions"})
            summary = (await c.get(r.headers["location"] + "/summary")).text
            assert "Runs the CMM" in summary and "GAUGE1" in summary
            await c.post(f"/api/budget/exclusions/{x.id}/delete")
            assert "Runs the CMM" not in (await c.get("/budget/")).text
            # the saved version keeps the list as it was
            assert "Runs the CMM" in (await c.get(r.headers["location"])).text


async def test_ai_reads_counts_but_never_money(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        with acting_as(OWNER):
            await c.post(
                "/api/budget/prices",
                data={"item": "Standard laptop", "category": "laptop", "unit_price": "1234"},
            )
        mcp = McpClient([module])
        with acting_as(Principal(OWNER.user, OWNER.display_name, OWNER.permissions, via_ai=True)):
            result = await mcp.call("budget_forecast_counts")
            assert "computers_in_use" in result and "1234" not in str(result)
            async with module.session() as s:
                with pytest.raises(budget.BudgetError):
                    await budget.save_version(s, module.current_principal(), "AI version")  # type: ignore[arg-type]


async def test_ai_proposes_prices_and_assumptions_and_a_person_approves(
    database_url: str, tmp_path: Path
) -> None:
    ai = Principal(OWNER.user, OWNER.display_name, OWNER.permissions, via_ai=True)
    mcp = McpClient([module])
    async with running(database_url, tmp_path) as c:
        with acting_as(ai):
            laptop = await mcp.call(
                "budget_propose_price",
                item="HP ProBook 4 G2a 16",
                category="laptop",
                unit_price="$1,332.84",
                source="HP quote Q3100327454",
                quoted_on="2026-09-25",
                note="Standard laptop with its 3-year care pack",
            )
            assert (
                laptop["review_url"].endswith("/budget/proposals") and "Waiting" in laptop["next"]
            )
            rule = await mcp.call(
                "budget_propose_assumptions",
                changes={"seat_rule": {"dept_groups": ["Engineering"]}, "contingency_pct": 15},
                note="Engineering hourly staff get computers",
            )
            junk = await mcp.call(
                "budget_propose_price",
                item="Dock",
                category="kit",
                unit_price="192",
                note="Too early",
            )
            for bad in (
                {"category": "server", "unit_price": "1"},
                {"category": "kit", "unit_price": "lots"},
                {"category": "kit", "unit_price": "1", "note": " "},
            ):
                with pytest.raises(ToolError):
                    await mcp.call("budget_propose_price", **{"item": "x", "note": "n", **bad})
            with pytest.raises(ToolError, match="Unknown assumption"):
                await mcp.call("budget_propose_assumptions", changes={"budget": 1}, note="n")
            assert len(await mcp.call("budget_list_proposals")) == 3
            assert await mcp.call("budget_list_prices") == []  # nothing applied yet
            # AI can't approve, through the page's actions or the service
            approve = f"/api/budget/proposals/{laptop['proposal']}/approve"
            assert (await c.post(approve)).status_code == 403
            async with module.session() as s:
                (first, *_) = await budget.proposals(s)
                with pytest.raises(budget.BudgetError):
                    await budget.approve_proposal(s, ai, first)

        with acting_as(READER):  # can see Budget, can't review proposals
            assert (await c.post(approve)).status_code == 403

        with acting_as(OWNER):
            page = (await c.get("/budget/assumptions")).text
            assert "HP ProBook 4 G2a 16" in page and "Engineering" in page
            assert "3 proposed changes are waiting" in (await c.get("/budget/")).text
            await c.post(approve)
            await c.post(f"/api/budget/proposals/{rule['proposal']}/approve")
            await c.post(f"/api/budget/proposals/{junk['proposal']}/reject")
            r = await c.post(approve)  # twice: refused, nothing applied again
            assert "already" in r.headers["location"]

        async with module.session() as s:
            (price,) = await budget.prices(s)
            assert (price.item, str(price.unit_price), price.updated_by) == (
                "HP ProBook 4 G2a 16",
                "1332.84",
                OWNER.user,
            )
            a = await budget.assumptions(s)
            assert a.seat_rule.dept_groups == ["Engineering"]
            assert a.seat_rule.pay_types == ["Salary"] and a.contingency_pct == 15
            assert await budget.proposals(s) == []

        with acting_as(ai):  # then a change and a removal of the approved price
            await mcp.call(
                "budget_propose_price",
                id=price.id,
                item=price.item,
                category="laptop",
                unit_price="1151.04",
                note="Without the care pack",
            )
            await mcp.call("budget_propose_price_removal", id=price.id, note="Dropped")
        with acting_as(OWNER):
            page = (await c.get("/budget/assumptions")).text
            assert "$1,332.84" in page and "$1,151.04" in page and "(removed)" in page
            async with module.session() as s:
                change, removal = await budget.proposals(s)
            await c.post(f"/api/budget/proposals/{change.id}/approve")
            async with module.session() as s:
                assert str((await budget.prices(s))[0].unit_price) == "1151.04"
            await c.post(f"/api/budget/proposals/{removal.id}/approve")
            async with module.session() as s:
                assert await budget.prices(s) == []


# ---- five-year plans (ADR 0020) -------------------------------------------------------------


def _aged_fleet(ages: dict[int, int] | None = None) -> list[dict[str, Any]]:
    """Computers by years old at TODAY: by default 10 each at 1, 4, 6 and 9 (the 9s must go)."""
    out: list[dict[str, Any]] = []
    for age, n in (ages or {1: 10, 4: 10, 6: 10, 9: 10}).items():
        bought = TODAY.replace(year=TODAY.year - age).isoformat()
        out += [device(f"PC{age}-{i}", "Workstation", bought) for i in range(n)]
    return out


def _plan(key: str, fleet: list[dict[str, Any]] | None = None, **changes: Any) -> engine.Forecast:
    return engine.forecast(
        fleet=fleet or _aged_fleet(),
        people=[person("Salary", "2020-01-01", f"{y}-06-01") for y in (2023, 2024, 2025)],
        purchases=None,
        prices=PRICES,
        assumptions=Assumptions(plan=key, **changes),
        today=TODAY,
    )


def _years(f: engine.Forecast, key: str | None = None) -> list[dict[str, Any]]:
    return next(p for p in f.plans if p["key"] == (key or f.plan))["years"]


def test_year_one_of_the_chosen_plan_is_the_budget() -> None:
    f = _plan("minimum")
    assert f.plan_start == 2027  # from July, the plan budgets next year
    assert {p["key"] for p in f.plans} == {"minimum", "even", "cycle", "amount", "lease"}
    y1 = _years(f)[0]
    assert y1["total"] == f.total and y1["must"] == 10 and y1["early"] == 0
    # One leaver a year, half of them reused: fewer bought, and a line says so.
    lines = {x.key: x for x in f.lines}
    assert y1["reused"] == 0.5 and lines["reuse"].count == -0.5
    assert lines["reuse"].amount == -450.0  # at the desktop price
    assert [y["year"] for y in _years(f)] == [2027, 2028, 2029, 2030, 2031]


def test_the_plans_differ_in_what_they_replace_early() -> None:
    cycle = _plan("cycle", steady_years=4)
    replaced = [y["must"] + y["failures"] + y["early"] for y in _years(cycle)]
    assert all(n >= 10 - 1e-6 for n in replaced)  # one in four a year, or the floor if more
    assert any(y["early"] > 0 for y in _years(cycle))

    amount = _plan("amount", plan_amount=20000)
    assert [y["total"] for y in _years(amount)][:2] == [pytest.approx(20000), pytest.approx(20000)]
    tight = _plan("amount", plan_amount=1000)  # less than the floor: the floor is still paid
    assert _years(tight)[0]["over"] > 0 and _years(tight)[0]["early"] == 0

    wave = _aged_fleet({1: 10, 2: 10, 6: 20})  # 20 reach 8 years together in 2029
    minimum, even = _plan("minimum", wave), _plan("even", wave)
    totals = [sum(y["total"] for y in _years(x)) for x in (minimum, even)]
    assert abs(totals[0] - totals[1]) / totals[0] < 0.05  # about the same five years
    peaks = [max(y["total"] for y in _years(x)) for x in (minimum, even)]
    assert peaks[1] < peaks[0]  # without Minimum's highest year


def test_a_leased_plan_pays_monthly_and_buys_nothing() -> None:
    f = _plan("lease", steady_years=4, lease_markup_pct=20)
    lines = {x.key: x for x in f.lines}
    assert lines["must_replace"].shown == "Leased" and lines["must_replace"].amount == 0
    y1 = _years(f)[0]
    assert lines["lease"].count == y1["leased"] == 11.7  # 10 must go, 2.2 fail, 0.5 reused
    assert lines["lease"].amount == round(11.7 * 900 / 4 * 1.2, 2)
    leased = [y["leased"] for y in _years(f)]
    assert leased == sorted(leased) and leased[-1] <= 40  # grows, never past the fleet


def test_each_plan_year_goes_by_ages_on_its_1_january() -> None:
    """From July the plan starts next year: a computer that turns 8 before 1 January must go
    in year 1 (the budget), though it's 7 today."""
    today = dt.date(2026, 10, 21)
    fleet = [device("OLD", "Workstation", "2018-12-01"), device("NEW", "Workstation", "2025-03-01")]
    f = engine.forecast(fleet=fleet, people=None, purchases=None, prices=PRICES,
                        assumptions=Assumptions(must_replace_age=8), today=today)  # fmt: skip
    assert f.plan_start == 2027
    assert [c.name for c in f.must_replace] == ["OLD"]
    assert f.must_replace[0].reasons == ["8.1 years old on 1 January 2027"]
    assert f.must_replace[0].age == 7.9  # shown as it is today
    lines = {x.key: x for x in f.lines}
    assert lines["must_replace"].count == 1
    assert lines["replacements"].count == pytest.approx(0.02)  # NEW, under 3 years: 2%
    years = _years(f)
    assert (years[0]["must"], years[0]["failures"], years[0]["total"]) == (1, 0.02, f.total)

    # Before July the plan is this year: ages as of 1 January this year.
    fleet = [d | {"last_seen": "2027-02-26"} for d in fleet]
    spring = engine.forecast(fleet=fleet, people=None, purchases=None, prices=PRICES,
                             assumptions=Assumptions(must_replace_age=8),
                             today=dt.date(2027, 3, 1))  # fmt: skip
    assert spring.plan_start == 2027 and [c.name for c in spring.must_replace] == ["OLD"]


def test_prices_rise_after_the_first_year() -> None:
    flat, rising = _plan("cycle", price_rise_pct=0), _plan("cycle", price_rise_pct=10)
    assert _years(flat)[0]["total"] == _years(rising)[0]["total"]
    assert _years(rising)[1]["peripherals"] == round(_years(flat)[1]["peripherals"] * 1.1, 2)


def test_bands_read_as_they_count_and_old_labels_still_load() -> None:
    assert engine.band_of(4.9) == "3 to 5 years" and engine.band_of(6.5) == "5 to 7 years"
    a = Assumptions.model_validate({"replace_rates": {"3 to 4 years": 7, "5 to 6 years": 16}})
    assert a.replace_rates == {"3 to 5 years": 7, "5 to 7 years": 16}


async def test_people_choose_a_plan_and_the_summary_shows_five_years(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _aged_fleet()

    async def rows(key: str, *args: Any, **kw: Any) -> list[dict[str, Any]] | None:
        return fleet if key == "assets.fleet" else None

    monkeypatch.setattr(budget, "_service_rows", rows)
    async with running(database_url, tmp_path) as c:
        with acting_as(OWNER):
            for p in PRICES:
                await c.post(
                    "/api/budget/prices",
                    data={"item": p.item, "category": p.category, "unit_price": str(p.unit_price)},
                )
            page = (await c.get("/budget/")).text
            assert "Five-year plan: Minimum" in page and "Every plan side by side" in page
            r = await c.post("/api/budget/plan", data={"plan": "amount", "must_replace_age": "8"})
            assert "error=" in r.headers["location"]  # the amount plan needs an amount
            r = await c.post(
                "/api/budget/plan",
                data={"plan": "even", "must_replace_age": "8", "reuse_pct": "100"},
            )
            assert r.headers["location"] == "/budget/#plan"
            page = (await c.get("/budget/")).text
            assert "Five-year plan: Even spend" in page
            r = await c.post("/api/budget/versions", data={"title": "2027 proposal"})
            summary = (await c.get(r.headers["location"] + "/summary")).text
            assert "Five-year outlook: the Even spend plan" in summary
            assert "If we spend less:" in summary and "The Minimum plan needs" in summary
        async with module.session() as s:
            a = await budget.assumptions(s)
            assert a.plan == "even" and a.reuse_pct == 100 and a.contingency_pct == 10
        with acting_as(READER):
            assert "Change the plan" not in (await c.get("/budget/")).text
            r = await c.post("/api/budget/plan", data={"plan": "cycle", "must_replace_age": "8"})
            assert r.status_code == 403


async def test_a_version_tracks_spend_in_its_budget_year(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Purchases count against a version only within the year it budgets, not from the day
    it was saved on."""
    today = dt.date.today()
    year = today.year + 1 if today.month >= 7 else today.year  # the plan's first year
    purchases = [
        {"date": f"{year}-06-01", "category": "laptop", "quantity": 1, "total": 1000},
        {"date": f"{year + 1}-01-02", "category": "laptop", "quantity": 1, "total": 10000},
    ]

    async def rows(key: str, *args: Any, **kw: Any) -> list[dict[str, Any]] | None:
        if key == "assets.fleet":
            return _aged_fleet()
        return purchases if args and args[0] == "device-purchases" else None

    monkeypatch.setattr(budget, "_service_rows", rows)
    async with running(database_url, tmp_path) as c:
        with acting_as(OWNER):
            r = await c.post("/api/budget/versions", data={"title": f"{year} proposal"})
            page = (await c.get(r.headers["location"])).text
    tile = re.search(rf"Spent in {year} \(purchase history\)</span>\s*<span[^>]*>([^<]*)<", page)
    assert tile and tile[1] == "$1,000.00"  # not the next year's purchase


# ---- the replacement list -------------------------------------------------------------------


def test_the_replacement_list_names_what_goes_and_a_skip_waits_a_year() -> None:
    skips = {"PC9-0": [(2027, "Light use", "paul@example.com", "9 years old")]}
    f = engine.forecast(
        fleet=_aged_fleet(),
        people=None,
        purchases=None,
        prices=PRICES,
        assumptions=Assumptions(),
        today=TODAY,
        skips=skips,
    )
    assert len(f.must_replace) == 9 and "PC9-0" not in {c.name for c in f.must_replace}
    (held,) = f.skipped
    assert held.name == "PC9-0" and held.skipped == "Light use" and held.reasons
    assert "1 computer skipped to 2028" in f.lines[0].note
    y1, y2 = _years(f)[:2]
    assert y1["must"] == 9 and y2["must"] == 1  # due again the year after
    # A skip from an earlier year is spent: it's back on the list, marked.
    again = engine.forecast(
        fleet=_aged_fleet(),
        people=None,
        purchases=None,
        prices=PRICES,
        assumptions=Assumptions(),
        today=TODAY,
        skips={"PC9-0": [(2026, "Light use", "paul@example.com", "8 years old")]},
    )
    (c,) = [c for c in again.must_replace if c.name == "PC9-0"]
    assert c.skipped_before == "Skipped from 2026: Light use" and not again.skipped


def test_the_plan_names_the_computers_it_replaces_early() -> None:
    wave = _aged_fleet({1: 10, 2: 10, 6: 20})
    f = _plan("even", wave)
    early = _years(f)[0]["early"]
    assert early > 1 and len(f.early) == round(early)
    assert all(c.name.startswith("PC6-") and "Even spend plan" in c.early for c in f.early)
    first = f.early[0]
    held = engine.forecast(
        fleet=wave,
        people=[person("Salary", "2020-01-01", f"{y}-06-01") for y in (2023, 2024, 2025)],
        purchases=None,
        prices=PRICES,
        assumptions=Assumptions(plan="even"),
        today=TODAY,
        skips={first.id: [(2027, "Shop floor, fine", "paul@example.com", first.early)]},
    )
    assert first.name in {c.name for c in held.skipped}
    assert first.name not in {c.name for c in held.early} and held.early  # the next oldest goes


async def test_a_person_skips_a_replacement_to_next_year_with_a_reason(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = [
        device("HOST01", "Workstation", "2015-01-01", model="Tower"),
        device("HOST02", "Workstation", "2015-01-01", model="Tower"),
        device("HOST03", "Workstation", "2025-01-01", model="Tower"),
    ]

    async def rows(key: str, *args: Any, **kw: Any) -> list[dict[str, Any]] | None:
        return fleet if key == "assets.fleet" else None

    monkeypatch.setattr(budget, "_service_rows", rows)
    async with running(database_url, tmp_path) as c:
        with acting_as(OWNER):
            page = (await c.get("/budget/")).text  # a summary that links to the list
            assert "Open the replacement list" in page and "Skip to" not in page
            page = (await c.get("/budget/replacements?skip=HOST01")).text
            assert "Replacement list for" in page and "Must go" in page
            assert '<option value="HOST01" selected>' in page
            r = await c.post("/api/budget/skips", data={"asset_id": "HOST01", "reason": ""})
            assert "error=" in r.headers["location"]
            r = await c.post("/api/budget/skips", data={"asset_id": "HOST03", "reason": "x"})
            assert "error=" in r.headers["location"]  # not on the list
            r = await c.post(
                "/api/budget/skips", data={"asset_id": "HOST01", "reason": "Used on Fridays"}
            )
            assert r.headers["location"] == "/budget/replacements"
            page = (await c.get("/budget/replacements")).text
            assert "Used on Fridays" in page and "Put back" in page
            r = await c.post("/api/budget/skips", data={"asset_id": "HOST01", "reason": "Again"})
            assert "already%20skipped" in r.headers["location"]
            r = await c.get("/api/budget/must-replace?format=csv")
            assert "Skipped to" in r.text and "Used on Fridays" in r.text and "HOST02" in r.text
        async with module.session() as s:
            f, _ = await budget.current(s)
            assert [x.name for x in f.must_replace] == ["HOST02"]
            assert [x.name for x in f.skipped] == ["HOST01"]
            (x,) = await budget.skips(s)
            assert x.due.endswith("years old on 1 January 2027") or "years old" in x.due
        with acting_as(Principal(OWNER.user, OWNER.display_name, OWNER.permissions, via_ai=True)):
            async with module.session() as s:
                with pytest.raises(budget.BudgetError):
                    await budget.skip(s, module.current_principal(), "HOST02", "AI")  # type: ignore[arg-type]
        with acting_as(READER):
            page = (await c.get("/budget/replacements")).text
            assert "Used on Fridays" in page and "Put back" not in page and "Skip to" not in page
            r = await c.post("/api/budget/skips", data={"asset_id": "HOST02", "reason": "x"})
            assert r.status_code == 403
        with acting_as(OWNER):
            r = await c.post("/api/budget/versions", data={"title": "With a skip"})
            summary = (await c.get(r.headers["location"] + "/summary")).text
            assert "Skipped to next year" in summary and "Used on Fridays" in summary
            assert "Replacement list" in summary and "HOST02" in summary
            await c.post(f"/api/budget/skips/{x.id}/delete")
            assert "Used on Fridays" not in (await c.get("/budget/replacements")).text
            assert (
                "Used on Fridays" in (await c.get(r.headers["location"])).text
            )  # the version keeps it
