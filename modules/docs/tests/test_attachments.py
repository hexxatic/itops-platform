"""Document attachments: versioned with the document, served as downloads."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from itops_docs import module, service
from sqlalchemy import text

from itops.sdk.content import Content, TextBlock
from itops.testing.app import McpClient, make_app

BODY = Content(blocks=[TextBlock(html="<p>See the attached runbook.</p>")]).model_dump_json()


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


def slug_of(r: httpx.Response) -> str:
    m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
    assert m, r.headers["location"]
    return m[1]


async def test_attachment_lifecycle(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        r = await c.post(
            "/api/docs/documents",
            data={"title": "Visio diagrams", "collection_id": "1", "content": BODY},
            files=[("attachments", ("Network.vsdx", b"VSDX", "application/octet-stream")),
                   ("attachments", ("Runbook.pdf", b"%PDF-1.7", "application/pdf"))],
        )  # fmt: skip
        slug = slug_of(r)
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "Attachments (2)" in page and "Network.vsdx" in page and "Runbook.pdf" in page
        url = re.search(r'href="(/files/[0-9a-f-]+)" download[^>]*>Runbook.pdf', page)
        assert url
        d = await c.get(url[1])
        assert d.content == b"%PDF-1.7" and d.headers["content-disposition"].startswith(
            "attachment"
        )
        await c.post(f"/api/docs/documents/{slug}/publish")

        # A draft adds one and removes another; readers keep the published list.
        async with module.session() as s:
            doc = await service.by_slug(s, slug)
            assert doc is not None
            v = await service.view(s, doc)
            vsdx = next(
                a
                for a in service.version_attachments(v.published)
                if a["filename"] == "Network.vsdx"
            )
        r = await c.post(
            f"/api/docs/documents/{slug}/save",
            data={"content": BODY, "remove_attachment": vsdx["id"]},
            files=[("attachments", ("Quote.msg", b"MSG", "application/vnd.ms-outlook"))],
        )
        assert r.status_code == 303
        reader = (await c.get(f"/docs/d/{slug}")).text
        assert "Network.vsdx" in reader and "Quote.msg" not in reader  # still the published list
        draft = (await c.get(f"/docs/d/{slug}?draft=1")).text
        assert "Quote.msg" in draft and "Network.vsdx" not in draft
        edit = (await c.get(f"/docs/d/{slug}/edit")).text
        assert 'name="remove_attachment"' in edit and "Quote.msg" in edit

        # A content-only save (no attachment inputs) keeps the list.
        mcp = McpClient([module])
        await mcp.call("docs_update_document", slug=slug, change_note="text", markdown="Updated")
        doc_json = await mcp.call("docs_get_document", slug=slug, include_draft=True)
        names = sorted(a["filename"] for a in doc_json["attachments"])
        assert names == ["Quote.msg", "Runbook.pdf"]
        assert doc_json["attachments"][0]["url"].startswith("http")

        await c.post(f"/api/docs/documents/{slug}/publish")
        async with module.session() as s:
            doc = await service.by_slug(s, slug)
            assert doc is not None
            passage = (await s.execute(text(
                "SELECT text FROM itops.search_passages WHERE module = 'docs' AND entity_id = :d"
                " AND label = 'Attachments'"), {"d": str(doc.id)})).scalar()  # fmt: skip
        assert passage and "Quote.msg" in passage and "Runbook.pdf" in passage


async def test_disallowed_attachment_is_refused(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        r = await c.post(
            "/api/docs/documents",
            data={"title": "Has an exe", "collection_id": "1", "content": BODY},
            files=[("attachments", ("setup.exe", b"MZ", "application/octet-stream"))],
        )
        assert r.headers["location"].startswith("/docs/new")
        assert "isn%27t%20allowed" in r.headers["location"] or "isn't" in r.headers["location"]
