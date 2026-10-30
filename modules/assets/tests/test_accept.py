"""A disagreement a person accepts as expected: hidden while its facts hold, back when they
change; never by AI."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from itops_assets import module, service
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, make_app

PERSON = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
AI = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}),
               via_ai=True, client="mcp")  # fmt: skip


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM assets.records"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


async def device(name: str, **fields: Any) -> str:
    async with module.session() as s:
        with acting_as(PERSON):
            r = await service.create(s, PERSON, type_key="device", name=name, fields=fields)
        await s.commit()
        return str(r.id)


async def set_fields(record_id: str, **fields: Any) -> None:
    async with module.session() as s:
        with acting_as(PERSON):
            r = await service.get(s, record_id)
            assert r is not None
            await service.update(s, PERSON, r, name=r.name,
                                 fields=service.record_fields(r) | fields)  # fmt: skip
        await s.commit()


async def listed() -> dict[str, Any]:
    with acting_as(PERSON):
        return {d["name"]: d for d in await service.fleet()}


async def test_accept_as_is(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        # A lab PC, on the network but meant to be off the domain.
        lab = await device("LAB01", kind="Workstation", status="Active",
                           last_seen="2099-01-01", ad_last_sign_in="2020-01-01",
                           ad_account="Enabled")  # fmt: skip
        assert (await listed())["LAB01"]["disagreements"]
        page = (await c.get(f"/assets/r/{lab}")).text
        assert "Accept a warning as is" in page and 'value="scan_vs_ad_stale"' in page

        no_reason = await c.post(f"/api/assets/records/{lab}/accept",
                                 data={"code": "scan_vs_ad_stale", "reason": " "},
                                 follow_redirects=False)  # fmt: skip
        assert "error=" in no_reason.headers["location"]
        done = await c.post(f"/api/assets/records/{lab}/accept",
                            data={"code": "scan_vs_ad_stale", "reason": "lab PC, off the domain"},
                            follow_redirects=False)  # fmt: skip
        assert done.status_code == 303
        lab_now = (await listed())["LAB01"]
        assert lab_now["disagreements"] == []
        assert lab_now["accepted"][0]["reason"] == "lab PC, off the domain"
        assert "LAB01" not in (await c.get("/assets/disagreements")).text
        assert "lab PC, off the domain" in (await c.get("/assets/disagreements?accepted=1")).text

        # A newer scan date: same facts, still accepted.
        await set_fields(lab, last_seen="2099-01-08")
        assert (await listed())["LAB01"]["disagreements"] == []
        # Its AD account is disabled: a different situation, so it shows again.
        await set_fields(lab, ad_account="Disabled")
        assert (await listed())["LAB01"]["disagreements"]
        # Disabled and back to enabled: the old acceptance was dropped, not revived.
        await set_fields(lab, ad_account="Enabled")
        assert (await listed())["LAB01"]["disagreements"]

        async with module.session() as s:
            record = await service.get(s, lab)
            assert record is not None
            with acting_as(AI), pytest.raises(service.AssetsError, match="AI connection"):
                await service.accept_finding(s, AI, record, "scan_vs_ad_stale", "x")
            with acting_as(PERSON), pytest.raises(service.AssetsError, match="any more"):
                await service.accept_finding(s, PERSON, record, "active_not_seen", "x")
