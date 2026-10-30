"""Budget forecasts from Assets' fleet and Reports' HR and purchase datasets (ADR 0016),
through their services only, and shows up on the Reports page.

Integration tests load several modules together, so they live outside any one module."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import itops_assets
import itops_budget
import itops_reports
from itops_assets import service as assets
from itops_budget import service as budget
from itops_reports import service as reports
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, connector_run, make_app

ADMIN = Principal("admin@example.com", "Admin", frozenset({"*"}))
NO_BUDGET = Principal("tech@example.com", "Tech", frozenset({"reports.read", "assets.read"}))
MODULES = [itops_assets.module, itops_reports.module, itops_budget.module]
TODAY = dt.date.today()


def years_ago(n: float) -> str:
    return str(TODAY - dt.timedelta(days=round(n * 365.25)))


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, MODULES, files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with app.state.runtime.sessionmaker() as s:
            for table in (
                "budget.versions",
                "budget.prices",
                "budget.settings",
                "reports.definitions",
                "reports.rows",
                "reports.datasets",
                "assets.records",
            ):
                await s.execute(text(f"DELETE FROM {table}"))
            await s.execute(text("DELETE FROM itops.source_refs WHERE module = 'assets'"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


async def computer(name: str, kind: str, **fields: Any) -> None:
    async with itops_assets.module.session() as s:
        await assets.create(
            s,
            ADMIN,
            type_key="device",
            name=name,
            fields={"kind": kind, "status": "Active", **fields},
        )
        await s.commit()


async def test_the_budget_forecasts_from_assets_and_reports(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        with acting_as(ADMIN):
            await computer("LAP01", "Laptop", purchased=years_ago(1))
            await computer("LAP02", "Laptop", warranty_expires=years_ago(6))  # bought ~9 years ago
            await computer(
                "DSK01",
                "Workstation",
                purchased=years_ago(4),
                operating_system="Windows 7 Professional",
            )
            await computer("DSK02", "Workstation")  # no dates: unknown age
            await computer("PRN01", "Printer", purchased=years_ago(9))
            last = TODAY.year - 1
            with connector_run("fake.hr", owns={"reports.rows": ["*"]}):
                await reports.ensure_dataset(
                    "hr-employees", "Employees", "employee", time_field="hire_date"
                )
                for i, (pay, hired) in enumerate(
                    [
                        ("Salary", f"{last}-03-01"),
                        ("Salary", f"{last}-06-01"),
                        ("Hourly", f"{last}-02-01"),
                    ]
                ):
                    await reports.upsert(
                        "hr-employees",
                        f"e{i}",
                        {"status": "Active", "pay_type": pay, "hire_date": hired},
                    )
            async with itops_budget.module.session() as s:
                await budget.add_price(
                    s, ADMIN, item="Laptop", category="laptop", unit_price="1000"
                )
                await budget.add_price(
                    s, ADMIN, item="Desktop", category="desktop", unit_price="800"
                )
                await budget.add_price(s, ADMIN, item="Dock", category="kit", unit_price="200")
                await s.commit()
                f, _ = await budget.current(s)

            assert f.computers_in_use == 4
            assert (f.by_class["standard_laptop"], f.by_class["standard_desktop"]) == (2, 2)
            assert sorted(c.name for c in f.must_replace) == ["DSK01", "LAP02"]
            assert [g["hires"] for g in f.growth][
                -1
            ] == 2  # salaried only: the hourly hire has no seat
            assert f.predicted_seats == 2 and f.total is not None

            assert "Device budget" in (await c.get("/budget/")).text
            page = (await c.get("/budget/replacements")).text
            assert "LAP02" in page and "Windows 7" in page
            export = await c.get("/api/budget/must-replace?format=csv")
            assert export.status_code == 200 and "DSK01" in export.text
            assert "Device budget" in (await c.get("/reports/")).text

        with acting_as(NO_BUDGET):  # Reports lists Budget only for people who may see it
            assert "Device budget" not in (await c.get("/reports/")).text
            assert (await c.get("/budget/")).status_code == 403
