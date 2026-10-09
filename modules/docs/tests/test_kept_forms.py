"""A failed save brings the form back filled in (not empty)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from itops_docs import module, service

from itops.sdk import Principal
from itops.sdk.content import Content, TextBlock
from itops.testing.app import acting_as, make_app

EMPTY = Content().model_dump_json()
OTHER = Principal(user="someone-else", display_name="Other", permissions=frozenset({"*"}))


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


async def test_failed_create_and_save_keep_what_was_typed(
    database_url: str, tmp_path: Path
) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as c:
        async with module.session() as s:
            col = await service.create_collection(s, name=f"Kept {tag}", description="")
            await s.commit()
            col_id = col.id

        # An empty document can't be created; the title, tags and file choice come back.
        r = await c.post(
            "/api/docs/documents",
            data={
                "title": f"Printer setup {tag}",
                "collection_id": str(col_id),
                "tags": "printers, howto",
                "content": EMPTY,
            },
            files={"attachments": ("notes.pdf", b"%PDF-1.4 x", "application/pdf")},
        )
        location = r.headers["location"]
        assert "error=The%20document%20is%20empty" in location and "kept=" in location
        page = (await c.get(location)).text
        assert f'value="Printer setup {tag}"' in page and 'value="printers, howto"' in page
        assert "choose the attachments again" in page
        with acting_as(OTHER):  # someone else with the same link sees an empty form
            assert f"Printer setup {tag}" not in (await c.get(location)).text

        # Saving an edit that empties the document keeps the change note.
        body = Content(blocks=[TextBlock(html="<p>Step one.</p>")]).model_dump_json()
        r = await c.post(
            "/api/docs/documents",
            data={"title": f"Scanner setup {tag}", "collection_id": str(col_id), "content": body},
        )
        m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
        assert m
        r = await c.post(
            f"/api/docs/documents/{m[1]}/save",
            data={"content": EMPTY, "change_note": "Reworded intro"},
        )
        assert "kept=" in r.headers["location"]
        assert 'value="Reworded intro"' in (await c.get(r.headers["location"])).text

        # A duplicate collection name keeps the description.
        r = await c.post(
            "/api/docs/collections",
            data={"name": f"Kept {tag}", "description": "All about printers"},
        )
        assert 'value="All about printers"' in (await c.get(r.headers["location"])).text
