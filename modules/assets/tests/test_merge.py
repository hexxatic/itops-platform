"""Merging two records that are one machine: a person merges, AI proposes (never retires)."""

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
from itops.testing.app import acting_as, connector_run, make_app

PERSON = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
AI = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}),
               via_ai=True, client="mcp")  # fmt: skip
OWNS = {"assets.device": ["model", "serial_number"]}


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM assets.proposals"))
            await s.execute(text("DELETE FROM assets.records"))
            await s.execute(text("DELETE FROM itops.source_refs WHERE module = 'assets'"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


async def sync(connector: str, source_type: str, source_id: str, name: str,
               fields: dict[str, Any], **ident: Any) -> str:  # fmt: skip
    who = Principal(user=f"connector:{connector}", display_name=connector,
                    permissions=frozenset({"*"}))  # fmt: skip
    with acting_as(who), connector_run(connector, owns=OWNS):
        done = await service.upsert(source_type=source_type, source_id=source_id,
                                    type_key="device", name=name, fields=fields,
                                    identity=ident)  # fmt: skip
    return str(done["id"])


async def create(type_key: str, name: str, **fields: Any) -> str:
    async with module.session() as s:
        with acting_as(PERSON):
            record = await service.create(s, PERSON, type_key=type_key, name=name, fields=fields)
        await s.commit()
        return str(record.id)


async def records(*ids: str) -> list[Any]:
    async with module.session() as s:
        with acting_as(PERSON):
            return [await service.get(s, i) for i in ids]


async def two_copies() -> tuple[str, str]:
    """HOST01 as an old documentation import (a purchase date, retired by mistake) and as
    the inventory's current, synced record; they never matched (a full vs short host name,
    the old one had no serial when the inventory first ran)."""
    old = await sync("fake.docs", "configuration", "7", "HOST01",
                     {"hostname": "host01.example.com", "purchased": "2022-09-01"},
                     hostname="host01.example.com")  # fmt: skip
    live = await sync("fake.inventory", "machine", "97751", "HOST01",
                      {"model": "M1", "serial_number": "SN-1", "status": "Active"},
                      serial="SN-1")  # fmt: skip
    assert old != live
    async with module.session() as s:
        with acting_as(PERSON):
            record = await service.get(s, old)
            assert record is not None
            values = service.record_fields(record) | {"status": "Retired", "serial_number": "SN-1"}
            await service.update(s, PERSON, record, name=record.name, fields=values)
        await s.commit()
    return old, live


async def test_a_person_merges_a_copy_into_the_record_kept(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path):
        old, live = await two_copies()
        to_old = {"module": "assets", "kind": "device", "id": old}
        screen = await create("monitor", "Monitor 1", connected_to=to_old)
        async with module.session() as s:
            with acting_as(PERSON):
                dup, kept = await service.get(s, old), await service.get(s, live)
                assert dup is not None and kept is not None
                assert [str(r.id) for r in await service.merge_candidates(s, dup)] == [live]
                assert await service.merge_plan(dup, kept) == ["hostname", "purchased"]
                filled = await service.merge(s, PERSON, dup, kept)
            await s.commit()
        assert filled == ["hostname", "purchased"]

        gone, kept, monitor = await records(old, live, screen)
        assert gone is None and kept is not None and monitor is not None
        values = service.record_fields(kept)
        # Blanks filled; its own status kept (the copy's "Retired" was never about it).
        assert (values["purchased"], values["status"]) == ("2022-09-01", "Active")
        assert service.record_fields(monitor)["connected_to"]["id"] == live
        systems = {x.connector for x in await module.refs.links("device", live)}
        assert systems == {"fake.docs", "fake.inventory"}
        # The documentation source's next run finds the record kept, not a new one.
        again = await sync("fake.docs", "configuration", "7", "HOST01",
                           {"hostname": "host01.example.com", "purchased": "2022-09-01"},
                           hostname="host01.example.com")  # fmt: skip
        assert again == live
        history = await module.audit.history(kind="device", entity_id=live)
        assert any("Merged HOST01 into this record" in (e.summary or "")
                   for e in history)  # fmt: skip


async def test_what_cant_be_merged(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path):
        old, live = await two_copies()
        # Two records of one source: merging them here would fight its next run.
        other = await sync("fake.inventory", "machine", "97752", "HOST02",
                           {"model": "M1", "serial_number": "SN-2"}, serial="SN-2")  # fmt: skip
        dup, kept, twin = await records(old, live, other)
        assert dup is not None and kept is not None and twin is not None
        with pytest.raises(service.AssetsError, match="Both are synced from"):
            await service.merge_plan(twin, kept)
        with pytest.raises(service.AssetsError, match="another record"):
            await service.merge_plan(kept, kept)
        async with module.session() as s:
            with acting_as(AI), pytest.raises(service.AssetsError, match="by a person"):
                await service.merge(s, AI, dup, kept)


async def test_ai_proposes_a_merge_and_a_person_approves(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path):
        old, live = await two_copies()
        async with module.session() as s:
            dup, kept = await service.get(s, old), await service.get(s, live)
            assert dup is not None and kept is not None
            with acting_as(AI):
                p = await service.propose_merge(s, AI, dup, kept, note="same serial SN-1")
                with pytest.raises(service.AssetsError, match="already proposed"):
                    await service.propose_merge(s, AI, dup, kept, note="again")
            await s.commit()
            assert (await records(old))[0] is not None  # nothing happens until approved
            with acting_as(AI), pytest.raises(service.AssetsError, match="by a person"):
                await service.decide(s, AI, p, approve=True)
            with acting_as(PERSON):
                result = await service.decide(s, PERSON, p, approve=True)
            await s.commit()
        assert result is not None and str(result.id) == live
        assert (await records(old))[0] is None


async def test_the_pages(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        old, live = await two_copies()
        page = (await c.get(f"/assets/r/{old}")).text
        assert "Merge into another record" in page
        assert f'value="{live}"' in page and "synced from fake.inventory" in page
        async with module.session() as s:
            dup, kept = await service.get(s, old), await service.get(s, live)
            assert dup is not None and kept is not None
            with acting_as(AI):
                await service.propose_merge(s, AI, dup, kept, note="same serial SN-1")
            await s.commit()
        proposals = (await c.get("/assets/proposals")).text
        assert "Merge HOST01 into HOST01" in proposals and "same serial SN-1" in proposals
        assert "Purchased" in proposals  # what the record kept gains
        done = await c.post(f"/api/assets/records/{old}/merge", data={"into": live},
                            follow_redirects=False)  # fmt: skip
        assert done.status_code == 303 and done.headers["location"].startswith(f"/assets/r/{live}")
        assert (await records(old))[0] is None
        assert "Nothing waiting" in (await c.get("/assets/proposals")).text


async def test_a_source_record_already_gone_doesnt_block_a_merge(
    database_url: str, tmp_path: Path
) -> None:
    # CBWS1045: the copy still held the inventory's old id for the machine (replaced, so
    # flagged missing); the record kept has the current one. Not a clash: the merge forgets
    # the old id instead of moving it, so the record kept isn't flagged "no longer in".
    async with running(database_url, tmp_path):
        old, live = await two_copies()
        await sync("fake.inventory", "machine", "97730", "HOST01", {"model": "M1"},
                   hostname="host01.example.com")  # fmt: skip
        async with module.session() as s:
            await s.execute(text("UPDATE itops.source_refs SET missing_since = sysutcdatetime()"
                                 " WHERE module = 'assets' AND source_id = '97730'"))  # fmt: skip
            await s.commit()
        assert {x.source_id for x in await module.refs.links("device", old)} == {"7", "97730"}
        assert old not in await module.refs.systems("device")  # the fakes aren't installed
        async with module.session() as s:
            dup, kept = await service.get(s, old), await service.get(s, live)
            assert dup is not None and kept is not None
            with acting_as(PERSON):
                await service.merge(s, PERSON, dup, kept)
            await s.commit()
        links = await module.refs.links("device", live)
        assert {x.source_id for x in links} == {"7", "97751"}
        assert all(x.missing_since is None for x in links)
