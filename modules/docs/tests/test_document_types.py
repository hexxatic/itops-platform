"""Document types (ADR 0008): typed fields on documents, through the web and MCP."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from itops_docs import module, service
from sqlalchemy import text

from itops.sdk.content import Content, TextBlock
from itops.testing.app import McpClient, ToolError, make_app

VENDOR: dict[str, Any] = {
    "name": "Vendor",
    "fields": [
        {"key": "account_number", "label": "Account number", "kind": "text", "required": True},
        {"key": "tier", "label": "Tier", "kind": "select", "options": ["Gold", "Silver"]},
        {"key": "renews", "label": "Renews", "kind": "date"},
        {"key": "portal_login", "label": "Portal login", "kind": "credential"},
        {"key": "runbook", "label": "Runbook", "kind": "link", "target": "docs"},
    ],
}


def body(html: str = "<p>Notes</p>") -> str:
    return Content(blocks=[TextBlock(html=html)]).model_dump_json()


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with runtime.sessionmaker() as s:  # a clean, published Vendor type
            await s.execute(text("DELETE FROM itops.record_types WHERE module = 'docs'"))
            await runtime.types.draft(s, "docs", VENDOR)
            await runtime.types.publish(s, "docs", "vendor")
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


async def create(c: httpx.AsyncClient, title: str, **fields: Any) -> httpx.Response:
    data = {"title": title, "collection_id": "1", "content": body(), "type_key": "vendor"}
    data |= {f"f__{k}": v for k, v in fields.items()}
    return await c.post("/api/docs/documents", data=data)


def slug_of(r: httpx.Response) -> str:
    m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
    assert m, r.headers["location"]
    return m[1]


async def test_typed_document_draft_publish(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        new_page = (await c.get("/docs/new?type=vendor")).text
        assert "Vendor details" in new_page and 'name="f__account_number"' in new_page

        # A draft may leave required fields empty...
        r = await create(c, "Acme Corp", tier="gold", renews="2027-03-01")
        slug = slug_of(r)
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "Vendor" in page and "Gold" in page and "2027-03-01" in page

        # ...but publishing needs them.
        r = await c.post(f"/api/docs/documents/{slug}/publish")
        assert "Before%20publishing" in r.headers["location"]
        assert "Account%20number%20is%20required" in r.headers["location"]

        r = await c.post(
            f"/api/docs/documents/{slug}/save",
            data={"content": body(), "f__account_number": "ACME-42", "f__tier": "Gold",
                  "f__renews": "2027-03-01", "f__portal_login": "op://IT/acme/password"},
        )  # fmt: skip
        assert r.status_code == 303
        assert (await c.post(f"/api/docs/documents/{slug}/publish")).status_code == 303
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "ACME-42" in page and "op://IT/acme/password" in page

        # Published values are queryable rows and searchable (not the credential).
        async with module.session() as s:
            doc = await service.by_slug(s, slug)
            assert doc is not None
            rows = await s.execute(text(
                "SELECT field_key, value_text, value_date FROM docs.document_fields"
                " WHERE document_id = :d ORDER BY field_key"), {"d": doc.id})  # fmt: skip
            fields = {r[0]: (r[1], r[2]) for r in rows.all()}
            passages = (await s.execute(text(
                "SELECT text FROM itops.search_passages WHERE module = 'docs' AND entity_id = :d"
                " AND label = 'Details'"), {"d": str(doc.id)})).scalars().all()  # fmt: skip
        assert fields["account_number"][0] == "ACME-42"
        assert str(fields["renews"][1]) == "2027-03-01"
        assert passages and "Account number: ACME-42" in passages[0]
        assert "op://" not in passages[0]


async def test_secrets_are_refused(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        r = await create(c, "Leaky vendor", portal_login="Hunter2!")
        assert "Never%20the%20password" in r.headers["location"]


async def test_content_edits_keep_fields_and_type_changes_keep_values(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        slug = slug_of(await create(c, "Keep me", account_number="K-1", tier="Silver"))
        mcp = McpClient([module])
        await mcp.call("docs_update_document", slug=slug, change_note="text only",
                       markdown="New text")  # fmt: skip
        doc = await mcp.call("docs_get_document", slug=slug, include_draft=True)
        assert doc["fields"] == {"account_number": "K-1", "tier": "Silver"}

        # Dropping the type keeps the stored values; restoring it brings them back.
        await c.post(f"/api/docs/documents/{slug}/details",
                     data={"title": "Keep me", "collection_id": "1", "type_key": "",
                           "change_type": "true"})  # fmt: skip
        assert "K-1" not in (await c.get(f"/docs/d/{slug}")).text
        await c.post(f"/api/docs/documents/{slug}/details",
                     data={"title": "Keep me", "collection_id": "1", "type_key": "vendor",
                           "change_type": "true"})  # fmt: skip
        assert "K-1" in (await c.get(f"/docs/d/{slug}")).text


async def test_links_to_documents_show_their_title(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        data = {"title": "Acme escalation runbook", "collection_id": "1", "content": body()}
        target = slug_of(await c.post("/api/docs/documents", data=data))
        async with module.session() as s:
            doc = await service.by_slug(s, target)
            assert doc is not None
            target_id = str(doc.id)
        edit_page = (await c.get("/docs/new?type=vendor")).text
        assert f"docs:document:{target_id}" in edit_page  # offered in the picker
        slug = slug_of(await create(c, "Acme with runbook", runbook=f"docs:document:{target_id}"))
        page = (await c.get(f"/docs/d/{slug}")).text
        assert f'href="/docs/d/{target}"' in page and "Acme escalation runbook" in page


async def test_ai_creates_typed_documents(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path):
        mcp = McpClient([module])
        made = await mcp.call(
            "docs_create_document", title="Globex", collection="vendors", markdown="Notes",
            type="vendor", fields={"account_number": "G-7", "renews": "2026-12-31"},
        )  # fmt: skip
        assert made["type"] == "vendor" and made["status"] == "draft"
        doc = await mcp.call("docs_get_document", slug=made["slug"], include_draft=True)
        assert doc["fields"] == {"account_number": "G-7", "renews": "2026-12-31"}
        listed = await mcp.call("docs_list_documents", type="vendor")
        assert made["slug"] in {d["slug"] for d in listed}
        bad = {"portal_login": "s3cret!"}
        try:
            await mcp.call("docs_create_document", title="Bad", collection="vendors",
                           markdown="x", type="vendor", fields=bad)  # fmt: skip
        except ToolError as exc:
            assert "Never the password" in str(exc)
        else:
            raise AssertionError("a secret in a credential field must be refused")
        tools = {t.name: t.description or "" for t in (await mcp._server.list_tools())}  # pyright: ignore[reportPrivateUsage]
        assert "1Password reference" in tools["docs_create_document"]  # guidance reaches the AI
        assert "markdown" in tools["docs_update_document"]


async def test_a_typed_document_can_be_fields_only(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        empty = Content(blocks=[TextBlock(html=" ")]).model_dump_json()
        r = await c.post("/api/docs/documents", data={
            "title": "Initech", "collection_id": "1", "content": empty,
            "type_key": "vendor", "f__account_number": "I-1"})  # fmt: skip
        page = (await c.get(r.headers["location"])).text
        assert "Initech" in page and "I-1" in page
