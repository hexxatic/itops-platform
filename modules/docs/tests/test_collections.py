"""Managing collections (admins only, human-only): create, rename, reorder, delete."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from itops_docs import module, service
from sqlalchemy import text

from itops.sdk import Principal
from itops.sdk.content import Content, TextBlock
from itops.testing.app import acting_as, make_app

EDITOR = Principal(user="ed", display_name="Ed", permissions=frozenset({"docs.read", "docs.write"}))


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


async def make_collection(c: httpx.AsyncClient, name: str) -> str:
    r = await c.post("/api/docs/collections", data={"name": name, "description": "Test"})
    assert r.headers["location"] == "/docs/collections", r.headers["location"]
    async with module.session() as s:
        cols = {col.name: col.slug for col, _ in await service.collections(s)}
    return cols[name]


async def test_create_rename_reorder_delete(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    first, second = f"Alpha {tag}", f"Beta {tag}"
    async with running(database_url, tmp_path) as c:
        a = await make_collection(c, first)
        b = await make_collection(c, second)
        page = (await c.get("/docs/collections")).text
        assert first in page and second in page
        assert "Manage collections" in (await c.get("/docs/")).text

        r = await c.post("/api/docs/collections", data={"name": first.upper()})
        assert "already%20a%20collection" in r.headers["location"]

        # Renaming keeps the web address.
        await c.post(f"/api/docs/collections/{a}", data={"name": f"Alpha renamed {tag}"})
        assert f"Alpha renamed {tag}" in (await c.get(f"/docs/c/{a}")).text

        async def order() -> list[str]:
            async with module.session() as s:
                return [col.slug for col, _ in await service.collections(s) if tag in col.name]

        assert await order() == [a, b]
        await c.post(f"/api/docs/collections/{b}/up")
        assert await order() == [b, a]

        # A collection with documents can't be deleted until they have somewhere to go.
        content = Content(blocks=[TextBlock(html="<p>x</p>")]).model_dump_json()
        async with module.session() as s:
            col_b = await service.collection_by_slug(s, b)
            col_a = await service.collection_by_slug(s, a)
            assert col_a and col_b
            a_id, b_id = col_a.id, col_b.id
        r = await c.post("/api/docs/documents", data={
            "title": f"Doc in {tag}", "collection_id": str(b_id), "content": content})  # fmt: skip
        m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
        assert m
        doc_slug = m[1]
        assert "1</strong> document" in (await c.get(f"/docs/collections/{b}/delete")).text
        r = await c.post(f"/api/docs/collections/{b}/delete")
        assert "Choose%20a%20collection" in r.headers["location"]
        r = await c.post(f"/api/docs/collections/{b}/delete", data={"move_to": str(a_id)})
        assert r.headers["location"] == "/docs/collections"
        async with module.session() as s:
            doc = await service.by_slug(s, doc_slug)
            assert doc is not None and doc.collection_id == a_id  # moved, not deleted
            assert await service.collection_by_slug(s, b) is None
            sql = text(
                "SELECT action FROM itops.audit_log WHERE module = 'docs'"
                " AND entity_kind = 'collection' AND entity_id IN (:a, :b) ORDER BY id"
            )
            rows = await s.execute(sql, {"a": str(a_id), "b": str(b_id)})
            actions = rows.scalars().all()
        assert {"collection.created", "collection.updated", "collection.deleted"} <= set(actions)

        # Empty: deletes straight away. (Tidy up the documents first.)
        async with module.session() as s:
            await s.execute(
                text("DELETE FROM docs.documents WHERE collection_id = :a"), {"a": a_id}
            )
            await s.commit()
        r = await c.post(f"/api/docs/collections/{a}/delete")
        assert r.headers["location"] == "/docs/collections"


async def test_only_admins_manage_collections(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        with acting_as(EDITOR):
            assert (await c.get("/docs/collections")).status_code == 403
            assert (await c.post("/api/docs/collections", data={"name": "Nope"})).status_code == 403
            assert "Manage collections" not in (await c.get("/docs/")).text
