"""Retiring a type asks what happens to its records; Docs and Assets do it through services."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import itops_assets
import itops_docs
from itops_assets import service as assets
from itops_docs import service as docs
from itops_docs.models import Document
from sqlalchemy import select

from itops.sdk import Principal
from itops.sdk.content import Content, TextBlock
from itops.testing.app import acting_as, make_app

ME = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))


def kind(name: str) -> dict[str, Any]:
    return {"name": name, "fields": [{"key": "owner", "label": "Owner", "kind": "text"}]}


async def make_types(runtime: Any, module: str, *names: str) -> list[str]:
    keys: list[str] = []
    async with runtime.sessionmaker() as s:
        for name in names:
            view = await runtime.types.draft(s, module, kind(name))
            await runtime.types.publish(s, module, view.key)
            keys.append(view.key)
        await s.commit()
    return keys


async def make_docs(type_key: str, n: int) -> list[str]:
    ids: list[str] = []
    async with itops_docs.module.session() as s:
        with acting_as(ME):
            for i in range(n):
                doc = await docs.create(
                    s, ME, collection_id=1, title=f"Doc {i} {type_key}", type_key=type_key,
                    content=Content(blocks=[TextBlock(html="<p>x</p>")]),
                    fields={"owner": "Pat"}, tags=[],
                )  # fmt: skip
                await docs.publish(s, ME, doc)
                ids.append(str(doc.id))
        await s.commit()
    return ids


async def doc_types(ids: list[str]) -> list[tuple[str | None, str]]:
    async with itops_docs.module.session() as s:
        found = await s.execute(
            select(Document).where(Document.id.in_([uuid.UUID(i) for i in ids]))
        )
        return sorted((d.type_key, d.status) for d in found.scalars())


async def test_retiring_moves_or_archives_documents(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    app = make_app(database_url, [itops_docs.module, itops_assets.module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        old, new, gone = await make_types(
            runtime, "docs", f"Old {tag}", f"New {tag}", f"Gone {tag}"
        )
        moving, archiving = await make_docs(old, 2), await make_docs(gone, 1)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            page = (await c.get(f"/docs/types/{old}/retire")).text
            assert "2 records" in page and f'value="move:{new}"' in page
            assert 'value="archive"' in page and 'value="keep"' in page

            r = await c.post(f"/api/docs/types/{old}/retire", data={"then": f"move:{new}"})
            assert r.status_code == 303 and "Moved%202%20record" in r.headers["location"]
            assert await doc_types(moving) == [(new, "published"), (new, "published")]
            async with runtime.sessionmaker() as s:
                assert (await runtime.types.get(s, "docs", old)).retired

            await c.post(f"/api/docs/types/{gone}/retire", data={"then": "archive"})
            assert await doc_types(archiving) == [(gone, "archived")]
            async with runtime.sessionmaker() as s:
                assert (await runtime.types.get(s, "docs", gone)).retired

            # Moving to a retired type is refused, and nothing is retired.
            (keep,) = await make_types(runtime, "docs", f"Keep {tag}")
            r = await c.post(f"/api/docs/types/{keep}/retire", data={"then": f"move:{old}"})
            assert "error=" in r.headers["location"]
            async with runtime.sessionmaker() as s:
                assert not (await runtime.types.get(s, "docs", keep)).retired


async def test_retiring_moves_asset_records(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    app = make_app(database_url, [itops_docs.module, itops_assets.module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        old, new = await make_types(runtime, "assets", f"Kit {tag}", f"Gear {tag}")
        async with itops_assets.module.session() as s:
            with acting_as(ME):
                record = await assets.create(s, ME, type_key=old, name=f"Box {tag}",
                                             fields={"owner": "Pat"})  # fmt: skip
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            page = (await c.get(f"/assets/types/{old}/retire")).text
            assert "1 record</strong> uses" in page and 'value="archive"' not in page
            await c.post(f"/api/assets/types/{old}/retire", data={"then": f"move:{new}"})
        async with itops_assets.module.session() as s:
            moved = await assets.get(s, record.id)
            assert moved is not None and moved.type_key == new
            assert assets.record_fields(moved) == {"owner": "Pat"}
