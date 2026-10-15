"""Logins: what a document logs into, kept in 1Password and only referenced (web and MCP)."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from itops_docs import module, service
from sqlalchemy import text

from itops.sdk.content import Content, TextBlock
from itops.testing.app import McpClient, ToolError, make_app, wait_for_search

LINK = "https://start.1password.com/open/i?a=EXAMPLE&i=keyence"
REF = "op://IT/Keyence/password"


def body(html: str = "<p>Log in as admin.</p>") -> str:
    return Content(blocks=[TextBlock(html=html)]).model_dump_json()


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


async def test_logins_are_versioned_references_never_secrets(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        assert 'name="login_ref"' in (await c.get("/docs/new")).text
        r = await c.post("/api/docs/documents", data={
            "title": "Keyence console", "collection_id": "1", "content": body(),
            "login_label": ["Keyence console (admin)", "", "Service account"],
            "login_ref": [LINK, "", REF],
        })  # fmt: skip
        slug = slug_of(r)
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "Logins (2)" in page and "Keyence console (admin)" in page
        assert "Open in 1Password" in page and REF in page

        # A pasted password is refused and the form comes back.
        r = await c.post(f"/api/docs/documents/{slug}/save", data={
            "content": body(), "login_label": ["Keyence console (admin)"],
            "login_ref": ["Hunter2!"],
        })  # fmt: skip
        assert "/edit?error=" in r.headers["location"]
        assert "Never%20the%20password" in r.headers["location"]

        # Clearing a row removes that login; publishing shows the rest to readers.
        r = await c.post(f"/api/docs/documents/{slug}/save", data={
            "content": body(), "login_label": ["Keyence console (admin)", ""],
            "login_ref": [LINK, ""],
        })  # fmt: skip
        assert "error" not in r.headers["location"]
        await c.post(f"/api/docs/documents/{slug}/publish")
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "Logins (1)" in page and REF not in page
        async with module.session() as s:
            doc = await service.by_slug(s, slug)
            assert doc is not None
            activity = await module.audit.history(kind="document", entity_id=str(doc.id))
        assert any("changed logins" in (a.summary or "") for a in activity)

        # References never go into search.
        await wait_for_search(module)
        async with module.session() as s:
            passages = (
                await s.execute(
                    text(
                        "SELECT text FROM itops.search_passages WHERE module = 'docs' "
                        "AND entity_id = :id"
                    ),
                    {"id": str(doc.id)},
                )
            ).scalars()
            assert all("1password" not in p and "op://" not in p for p in passages)


async def test_ai_lists_logins_and_content_edits_keep_them(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path):
        mcp = McpClient([module])
        made = await mcp.call(
            "docs_create_document", title="Megamation logins", collection="general",
            markdown="Log in as the shop account.",
            logins=[{"label": "Megamation (shop)", "ref": REF}],
        )  # fmt: skip
        slug = made["slug"]
        await mcp.call("docs_update_document", slug=slug, change_note="text only",
                       markdown="Log in as the shop account on the iPad.")  # fmt: skip
        doc = await mcp.call("docs_get_document", slug=slug, include_draft=True)
        assert doc["logins"] == [{"label": "Megamation (shop)", "ref": REF}]
        try:
            await mcp.call("docs_update_document", slug=slug, change_note="oops",
                           markdown="x", logins=[{"label": "Shop", "ref": "s3cret!"}])  # fmt: skip
        except ToolError as exc:
            assert "Never the password" in str(exc)
        else:
            raise AssertionError("a secret as a login must be refused")
        tools = {t.name: t.description or "" for t in (await mcp._server.list_tools())}  # pyright: ignore[reportPrivateUsage]
        assert "NEVER the password" in tools["docs_update_document"].split("`logins`")[1]
