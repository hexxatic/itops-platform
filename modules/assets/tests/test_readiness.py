"""OS readiness (ADR 0019): admin-kept OS targets, devices checked by their hardware."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from itops_assets import module, service
from itops_assets import readiness as rd
from itops_assets.service import AssetsError
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, connector_run, make_app

ADMIN = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
AI = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}),
               via_ai=True, client="mcp")  # fmt: skip
READER = Principal(user="rita", display_name="Rita", permissions=frozenset({"assets.read"}))
CONNECTOR = Principal(user="connector:fake.inventory", display_name="Inventory",
                      permissions=frozenset({"*"}))  # fmt: skip

W11 = rd.PRESETS["Windows 11"]
TARGET = rd.Target(
    "Windows 11", W11["upgrades_from"], rd.pattern_lines(W11["processors"]), 4, 64, "2.0"
)
GOOD = {"processor": "Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz", "memory_gb": 8,
        "storage_gb": 256, "tpm": "2.0"}  # fmt: skip


# ---- the rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cpu",
    [
        "Intel Core i5-8250U CPU @ 1.60GHz",
        "11th Gen Intel(R) Core(TM) i7-1185G7 @ 3.00GHz",
        "13th Gen Intel(R) Core(TM) i7-13700",
        "Intel(R) Core(TM) Ultra 7 155H",
        "Intel Xeon W-2133 CPU @ 3.60GHz",
        "Intel Xeon Silver 4210R CPU @ 2.40GHz",
        "Intel Xeon E-2224G CPU @ 3.50GHz",
        "AMD Ryzen 5 PRO 4650G with Radeon Graphics",
        "AMD Ryzen 5 2600 Six-Core Processor",
        "Intel(R) Celeron(R) N4020 CPU @ 1.10GHz",
        "Snapdragon(R) X Elite - X1E78100",
    ],
)
def test_windows_11_processors(cpu: str) -> None:
    assert rd.check(TARGET, GOOD | {"processor": cpu}).status == rd.READY


@pytest.mark.parametrize(
    "cpu",
    [
        "Intel Celeron CPU J3455 @ 1.50GHz",
        "Intel Pentium CPU G4400 @ 3.30GHz",
        "Intel Core i3-6100 CPU @ 3.70GHz",
        "Intel Core i5-7500 CPU @ 3.40GHz",
        "Intel(R) Core(TM) i7-4790 CPU",
        "AMD Ryzen 7 1700 Eight-Core Processor",
        "Intel(R) Xeon(R) CPU E5-2680 v4",
    ],
)
def test_older_processors_arent_ready(cpu: str) -> None:
    r = rd.check(TARGET, GOOD | {"processor": cpu})
    assert r.status == rd.NOT_READY and "processor list" in r.why[0]


def test_every_requirement_is_checked_and_explained() -> None:
    r = rd.check(TARGET, {"processor": "Intel Core i3-6100 CPU @ 3.70GHz", "memory_gb": 2,
                          "storage_gb": 32, "tpm": "1.2"})  # fmt: skip
    assert r.status == rd.NOT_READY
    assert r.why == [
        "Intel Core i3-6100 isn't on Windows 11's processor list",
        "2 GB memory, needs 4 GB",
        "32 GB storage, needs 64 GB",
        "TPM 1.2, needs 2.0",
    ]
    # Nothing fails but something's missing: unknown, with what's missing.
    r = rd.check(TARGET, GOOD | {"tpm": "", "storage_gb": None})
    fills = " (an inventory sync with hardware fills it in)"
    assert (r.status, r.why) == (
        rd.UNKNOWN, ["No storage size recorded" + fills, "No TPM recorded" + fills])  # fmt: skip
    # A failure beats a gap.
    assert rd.check(TARGET, {"tpm": "None"}).status == rd.NOT_READY


def test_a_target_applies_to_the_systems_it_replaces() -> None:
    assert rd.applies(TARGET, "Windows 10 Pro for Workstations")
    assert not rd.applies(TARGET, "Windows 11 Pro") and not rd.applies(TARGET, "")
    assert not rd.applies(TARGET, "Windows Server 2016 Standard")
    # The next Windows is data: a target that replaces 10 and 11.
    nxt = rd.Target("Windows 12", ["Windows 10", "Windows 11"], [r"Intel Core Ultra"], 16)
    devices = [{"operating_system": "Windows 11 Pro", **GOOD}]
    assert [r.as_dict() for r in rd.readiness([TARGET, nxt], devices[0])] == [
        {
            "target": "Windows 12",
            "status": "not_ready",
            "why": [
                "Intel Core i5-10500T isn't on Windows 12's processor list",
                "8 GB memory, needs 16 GB",
            ],
        }
    ]


def test_patterns_as_typed() -> None:
    assert rd.pattern_lines("a  # one\n\n# only a comment\n b ") == ["a", "b"]
    assert rd.bad_patterns(["ok", "(broken"]) == ["(broken"]
    assert (
        rd.clean_processor("Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz") == "Intel Xeon E5-2680 v4"
    )


# ---- the service and pages --------------------------------------------------------------------


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    # Fresh starter types, so Device has the hardware fields.
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app), module.session() as s:
        await s.execute(text("DELETE FROM itops.record_types WHERE module = 'assets'"))
        await s.commit()
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM assets.records"))
            await s.execute(text("DELETE FROM assets.os_targets"))
            await s.execute(text("DELETE FROM itops.source_refs WHERE module = 'assets'"))
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


async def test_targets_are_kept_by_admins(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path), module.session() as s:
        with acting_as(ADMIN):
            t = await service.add_preset(s, ADMIN, "Windows 11")
            await s.commit()
            assert service.target_of(t).upgrades_from[0] == "Windows 10"
            with pytest.raises(AssetsError, match="already a target"):
                await service.add_preset(s, ADMIN, "Windows 11")
            await s.rollback()
            with pytest.raises(AssetsError, match="aren't valid"):
                await service.save_target(s, ADMIN, None, name="Next", upgrades_from="Windows 11",
                                          processors="(broken")  # fmt: skip
            with pytest.raises(AssetsError, match="which systems it replaces"):
                await service.save_target(s, ADMIN, None, name="Next", upgrades_from=" ",
                                          processors="Intel")  # fmt: skip
            with pytest.raises(AssetsError, match="at least one requirement"):
                await service.save_target(s, ADMIN, None, name="Next", upgrades_from="Windows 11",
                                          processors="# nothing")  # fmt: skip
        with acting_as(AI), pytest.raises(AssetsError, match="done by a person"):
            await service.save_target(s, AI, None, name="Next", upgrades_from="Windows 11",
                                      processors="Intel")  # fmt: skip


async def test_devices_are_checked_and_budget_sees_it(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        ready = await device("PC1", kind="Workstation", operating_system="Windows 10 Pro", **GOOD)
        await device("PC2", kind="Laptop", operating_system="Windows 10 Pro",
                     processor="Intel Core i5-6300U CPU @ 2.40GHz", memory_gb=8, tpm="2.0",
                     storage_gb=256)  # fmt: skip
        await device("PC3", kind="Laptop", operating_system="Windows 10 Pro")
        await device("PC4", kind="Laptop", operating_system="Windows 11 Pro", **GOOD)
        await device("OLD", kind="Laptop", status="Retired", operating_system="Windows 10")

        # No target yet: nothing to check against.
        assert await service.readiness_report() == []
        page = (await c.get("/assets/readiness")).text
        assert "No operating systems to check against yet" in page and "Add Windows 11" in page
        assert (
            await c.post("/api/assets/readiness/presets", data={"name": "Windows 11"})
        ).status_code == 303

        (report,) = await service.readiness_report()
        assert report["counts"] == {"ready": 1, "not_ready": 1, "unknown": 1}
        assert [d["name"] for d in report["devices"]] == ["PC1", "PC2", "PC3"]
        fleet = {d["name"]: d for d in await service.fleet()}
        assert fleet["PC1"]["os_ready"] == [{"target": "Windows 11", "status": "ready", "why": []}]
        assert fleet["PC2"]["os_ready"][0]["status"] == "not_ready"
        assert fleet["PC4"]["os_ready"] == [] and fleet["PC1"]["processor"].startswith("Intel")

        page = (await c.get("/assets/readiness")).text
        assert "Intel Core i5-6300U isn&#39;t on Windows 11&#39;s processor list" in page
        narrowed = (await c.get(f"/assets/readiness?target={report['id']}&show=ready")).text
        assert "PC1" in narrowed and "PC2" not in narrowed and "Showing ready only" in narrowed
        assert "Can it run" in (await c.get(f"/assets/r/{ready}")).text
        csv = await c.get(f"/api/assets/readiness/{report['id']}?format=csv")
        assert csv.status_code == 200 and "Ready for Windows 11" in csv.text

        # The requirements are the admin's to change; a reader sees, can't change.
        edit = await c.get(f"/assets/readiness/targets/{report['id']}")
        assert edit.status_code == 200 and "Supported processors" in edit.text
        r = await c.post(f"/api/assets/readiness/targets/{report['id']}", data={
            "name": "Windows 11", "upgrades_from": "Windows 10", "processors": W11["processors"],
            "min_memory_gb": "16", "min_tpm": "2.0"})  # fmt: skip
        assert r.status_code == 303
        (report,) = await service.readiness_report()
        assert report["counts"]["ready"] == 0  # PC1 has 8 GB
        with acting_as(READER):
            assert (await c.get("/assets/readiness")).status_code == 200
            assert (await c.get(f"/assets/readiness/targets/{report['id']}")).status_code == 403
            r = await c.post("/api/assets/readiness/presets", data={"name": "Windows 11"})
            assert r.status_code == 403
        with acting_as(AI):  # human-only, even with every permission
            r = await c.post(f"/api/assets/readiness/targets/{report['id']}/delete")
            assert r.status_code == 403
        r = await c.post(f"/api/assets/readiness/targets/{report['id']}/delete")
        assert r.status_code == 303 and await service.readiness_report() == []


async def test_a_sync_skips_fields_the_type_lacks(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path):
        owns = {"assets.device": ["model", "processor", "gpu"]}
        with acting_as(CONNECTOR), connector_run("fake.inventory", owns=owns):
            result = await service.upsert(
                source_type="machine", source_id="1", type_key="device", name="PC9",
                fields={"model": "M1", "processor": "Intel Core i5-8500", "gpu": "Big"},
            )  # fmt: skip
        assert result["outcome"] == "created" and result["ignored"] == ["gpu"]
        async with module.session() as s:
            record = await service.get(s, result["id"])
            assert record is not None
            assert service.record_fields(record)["processor"] == "Intel Core i5-8500"
