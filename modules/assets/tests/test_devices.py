"""The Devices list (filters, review views, paging, download, bulk change) and page."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from itops_assets import devices as dv
from itops_assets import module, service
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, make_app

ADMIN = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
READER = Principal(user="rita", display_name="Rita", permissions=frozenset({"assets.read"}))
AI = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}),
               via_ai=True, client="mcp")  # fmt: skip
TODAY = dt.date(2026, 10, 20)


def test_filters_and_views() -> None:
    d: dict[str, Any] = {"name": "HOST01", "kind": "Laptop", "status": "", "model": "",
         "sources": ["IT Glue"], "seen_by": "", "disagreements": []}  # fmt: skip
    assert dv.sources_of(d) == ["IT Glue"]
    no_model = dv.Filters.from_query({"view": "no_model"})
    assert no_model.model == "none" and dv.matches(d, no_model)
    assert dv.matches(d, dv.Filters.from_query({"source": "only:IT Glue", "inuse": "none"}))
    assert not dv.matches(d, dv.Filters.from_query({"source": "not:IT Glue"}))
    assert not dv.matches(d, dv.Filters.from_query({"source": "none"}))
    assert not dv.matches(d, dv.Filters.from_query({"status": "Active"}))
    assert dv.matches(d, dv.Filters.from_query({"status": "none", "cls": "none"}))
    assert dv.Filters.from_query({"q": "x", "page": "3"}).query(page=4) == {"q": "x", "page": "4"}


def test_rows_say_what_is_worked_out() -> None:
    d: dict[str, Any] = {
         "name": "HOST01", "url": "/assets/r/1", "computer_class": "Standard Laptop",
         "class_basis": "set", "status": "Active", "seen_by": "ad",
         "ad_last_sign_in": "2026-10-19", "acquired": "2024-10-20", "age_basis": "serial",
         "ad_account": "Enabled", "sources": ["Active Directory"],
         "disagreements": ["x"]}  # fmt: skip
    r = dv.row(d, TODAY)
    assert r["class"] == "Standard Laptop *" and r["seen"] == "AD only (2026-10-19)"
    assert r["age"] == "2 y (HP serial (estimate))" and r["sources"] == "Active Directory"
    assert r["flag"] == {"badge": "Disagree", "tone": "warning"}


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app), module.session() as s:
        await s.execute(text("DELETE FROM itops.record_types WHERE module = 'assets'"))
        await s.commit()
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM assets.records"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


async def device(name: str, **fields: Any) -> str:
    async with module.session() as s:
        with acting_as(ADMIN):
            r = await service.create(s, ADMIN, type_key="device", name=name, fields=fields)
        await s.commit()
        return str(r.id)


async def test_review_a_set_and_change_it_at_once(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        await device("PC1", kind="Laptop", model="ProBook 450", status="Active")
        await device("PC2", kind="Laptop")  # no model, no status
        await device("PC3", kind="Workstation")  # no model, no status
        pid = await device("PC4", kind="Workstation", model="OptiPlex 3080", status="Ignored",
                           serial_number="ABC123", ad_account="Enabled")  # fmt: skip
        with acting_as(READER):
            page = (await c.get("/assets/t/device")).text
            assert "4 devices" in page and "Review:" in page
            page = (await c.get("/assets/t/device?view=no_model")).text
            assert "2 devices" in page and "PC2" in page and "PC1" not in page
            assert "Change all" not in page  # readers can't
            r = await c.get("/api/assets/devices/export?model=none&format=csv")
            assert r.status_code == 200 and "PC2" in r.text and "PC1" not in r.text
            retire = {"model": "none", "expected": "2", "set_status": "Retired"}
            r = await c.post("/api/assets/devices/bulk", data=retire)
            assert r.status_code == 403
        with acting_as(ADMIN):
            # The set changed since the page was shown: nothing happens.
            r = await c.post("/api/assets/devices/bulk", data=retire | {"expected": "5"})
            assert "error=" in r.headers["location"]
            r = await c.post("/api/assets/devices/bulk", data={
                "model": "none", "expected": "2", "set_status": "Retired",
                "set_class": "Engineering Laptop"})  # fmt: skip
            assert r.headers["location"].endswith("done=2")
            found = {d["name"]: d for d in await service.fleet(include_ignored=True)}
            assert found["PC2"]["status"] == found["PC3"]["status"] == "Retired"
            assert found["PC2"]["computer_class"] == "Engineering Laptop"
            assert found["PC1"]["status"] == "Active"  # not in the set
            page = (await c.get(f"/assets/r/{pid}")).text
            assert "At a glance" in page and "Identity" in page and "Active Directory" in page
            assert "Details" not in page  # devices get sections
        with acting_as(AI):
            async with module.session() as s:
                try:
                    await service.set_devices(s, AI, [pid], status="Retired")
                    raise AssertionError("AI changed devices directly")
                except service.AssetsError:
                    pass
