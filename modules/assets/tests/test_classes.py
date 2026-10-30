"""Computer classes: Standard or Engineering, laptop or desktop, by model or set by a person."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from itops_assets import module, service
from itops_assets.classes import PRESETS, Rules, classify, tier_of
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, make_app

ADMIN = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
READER = Principal(user="rita", display_name="Rita", permissions=frozenset({"assets.read"}))
RULES = Rules.from_text(PRESETS)


def cls(kind: str, model: str, **more: Any) -> str | None:
    return classify({"kind": kind, "model": model, **more}, RULES)[0]


def test_the_presets_sort_common_models() -> None:
    assert cls("Laptop", "HP ZBook X G1i 16 inch Mobile Workstation PC") == "Engineering Laptop"
    assert cls("Laptop", "Precision 7560") == "Engineering Laptop"
    assert cls("Laptop", "Precision 3560") == "Standard Laptop"  # issued as standard
    assert cls("Workstation", "Precision 7920 Tower") == "Engineering Desktop"
    assert cls("Workstation", "HP Z4 G4 Workstation") == "Engineering Desktop"
    assert cls("Workstation", "OptiPlex 3080") == "Standard Desktop"
    assert cls("Workstation", "HP Elite Mini 800 G9 Desktop PC") == "Standard Desktop"


def test_a_laptop_by_name_is_a_laptop_whatever_the_scan_said() -> None:
    assert cls("Workstation", "HP ProBook 460 16 inch G11 Notebook PC") == "Standard Laptop"


def test_new_models_and_other_kinds_have_no_class() -> None:
    assert cls("Laptop", "Brand New 9000") is None
    assert cls("Laptop", "") is None
    assert cls("Printer", "OptiPlex 3080") is None
    assert tier_of("anything", Rules({"Engineering": ["(bad"], "Standard": ["any"]})) == "Standard"


def test_a_person_overrides_the_model() -> None:
    assert classify({"kind": "Laptop", "model": "Precision 3560",
                     "computer_class": "Engineering Laptop"}, RULES) == (
        "Engineering Laptop", "set")  # fmt: skip
    assert classify({"kind": "Laptop", "model": "Precision 3560"}, RULES) == (
        "Standard Laptop", "model")  # fmt: skip


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
            await s.execute(text("DELETE FROM assets.computer_classes"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


async def device(name: str, **fields: Any) -> None:
    async with module.session() as s:
        with acting_as(ADMIN):
            await service.create(s, ADMIN, type_key="device", name=name, fields=fields)
        await s.commit()


async def test_admins_keep_the_lists_and_the_fleet_carries_the_class(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        await device("PC1", kind="Laptop", model="Precision 3560")
        await device("PC2", kind="Laptop", model="Brand New 9000")
        await device("PC3", kind="Laptop", model="Precision 3560",
                     computer_class="Engineering Laptop")  # fmt: skip
        with acting_as(READER):
            page = (await c.get("/assets/classes")).text
            assert "1 computer in use has a model on neither list" in page
            assert "Admins change these." in page
            r = await c.post("/api/assets/classes", data={"engineering": "x", "standard": "y"})
            assert r.status_code == 403
        with acting_as(ADMIN):
            r = await c.post("/api/assets/classes", data={"engineering": "(bad", "standard": ""})
            assert "error=" in r.headers["location"]
            r = await c.post("/api/assets/classes", data={
                "engineering": PRESETS["Engineering"] + "\nBrand New",
                "standard": PRESETS["Standard"]})  # fmt: skip
            assert r.headers["location"] == "/assets/classes"
            found = {d["name"]: d for d in await service.fleet()}
        assert (found["PC1"]["computer_class"], found["PC1"]["class_basis"]) == (
            "Standard Laptop", "model")  # fmt: skip
        assert found["PC2"]["computer_class"] == "Engineering Laptop"  # the new pattern
        assert (found["PC3"]["computer_class"], found["PC3"]["class_basis"]) == (
            "Engineering Laptop", "set")  # fmt: skip
