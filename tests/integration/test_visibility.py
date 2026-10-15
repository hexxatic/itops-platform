"""Visibility of areas by AD group (ADR 0012), across Docs, Assets, search, files and links.

A restricted area is invisible to anyone outside its groups (admins excepted): missing from
lists, search, pickers and MCP tools, its pages and files "not found", and links to it shown
as "Restricted". An area kept out of AI is invisible to every AI connection, admins
included, and unchanged in the browser."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import itops_assets
import itops_docs
import pytest
from itops_assets import service as assets
from itops_docs import service as docs
from sqlalchemy import text

from itops.sdk import Principal
from itops.sdk.content import Content, TextBlock
from itops.testing.app import McpClient, ToolError, acting_as, make_app, wait_for_search

MODULES = [itops_docs.module, itops_assets.module]
GROUP = "App-ITOps-Security"
ADMIN = Principal(user="admin@example.com", display_name="Admin", permissions=frozenset({"*"}))
_WORK = frozenset({"docs.read", "docs.write", "assets.read", "assets.write"})
READER = Principal(user="reader@example.com", display_name="Reader", permissions=_WORK)
MEMBER = Principal(
    user="member@example.com", display_name="Member", permissions=_WORK,
    groups=frozenset({GROUP.lower()}),
)  # fmt: skip
NOTE: dict[str, Any] = {
    "name": "Security note",
    "fields": [{"key": "device", "label": "Device", "kind": "link", "target": "assets"}],
}


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, Any]]:
    app = make_app(database_url, MODULES, files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with runtime.sessionmaker() as s:
            await s.execute(text("DELETE FROM itops.area_restrictions"))
            if await runtime.types.get(s, "docs", "security_note") is None:
                await runtime.types.draft(s, "docs", NOTE)
                await runtime.types.publish(s, "docs", "security_note")
            await s.commit()
        await runtime.areas.reload()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            try:
                yield c, app
            finally:
                async with runtime.sessionmaker() as s:
                    await s.execute(text("DELETE FROM itops.area_restrictions"))
                    await s.commit()


async def _restrict(app: Any, module: str, kind: str, key: str, **how: Any) -> None:
    with acting_as(ADMIN):
        await app.state.runtime.areas.set(module, kind, key, name=key, **how)


async def test_a_restricted_collection_is_invisible_outside_its_groups(
    database_url: str, tmp_path: Path
) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        with acting_as(ADMIN):
            async with itops_docs.module.session() as s:
                col = await docs.create_collection(s, name=f"Security {tag}")
                await s.commit()
                col_id, col_slug = col.id, col.slug
            stored = await itops_docs.module.files.save(
                b"%PDF-1.4 playbook", mime_type="application/pdf", filename="playbook.pdf",
                attachment=True,
            )  # fmt: skip
            async with itops_docs.module.session() as s:
                doc = await docs.create(
                    s, ADMIN, collection_id=col_id, title=f"Ransomware playbook {tag}",
                    content=Content(blocks=[TextBlock(html=f"<p>Isolate first {tag}.</p>")]),
                    attachments=[{"id": stored.id, "filename": "playbook.pdf",
                                  "size": stored.size_bytes, "mime": stored.mime_type}],
                )  # fmt: skip
                await docs.publish(s, ADMIN, doc)
                await s.commit()
                slug, doc_id = doc.slug, doc.id
            async with itops_assets.module.session() as s:
                fw = await assets.create(s, ADMIN, type_key="device", name=f"FW-{tag}")
                await s.commit()
        await wait_for_search(itops_docs.module)
        async with app.state.runtime.sessionmaker() as s:  # as indexed before areas existed
            await s.execute(text("UPDATE itops.search_passages SET area = NULL"
                                 " WHERE entity_id = :e"), {"e": str(doc_id)})  # fmt: skip
            await s.commit()
        await _restrict(app, "docs", "collection", str(col_id), groups=[GROUP], no_ai=False)

        # Outside the group: the collection, its document, its files and its words are gone.
        with acting_as(READER):
            assert f"Security {tag}" not in (await c.get("/docs/")).text
            assert (await c.get(f"/docs/c/{col_slug}")).status_code == 404
            assert (await c.get(f"/docs/d/{slug}")).status_code == 404
            assert (await c.get(f"/files/{stored.id}")).status_code == 404
            assert f"Ransomware playbook {tag}" not in (await c.get(f"/search?q={tag}")).text
            mcp = McpClient(MODULES, app=app)
            slugs = [x["slug"] for x in await mcp.call("docs_list_collections")]
            assert col_slug not in slugs
            with pytest.raises(ToolError, match="No document"):
                await mcp.call("docs_get_document", slug=slug)
            with pytest.raises(ToolError, match="Unknown collection"):
                await mcp.call("docs_create_document", title="x", collection=col_slug,
                               markdown="x")  # fmt: skip
            assert [h for h in await mcp.call("itops_search", query=tag)
                          if h["title"].startswith("Ransomware")] == []  # fmt: skip
            # A link to it from somewhere they can see says "Restricted", no title.
            async with itops_assets.module.session() as s:
                assert (await assets.related(f"docs:document:{doc_id}")) == []
            looked_up = await docs.lookup(ids=[str(doc_id)])
            assert looked_up == [{"id": str(doc_id), "kind": "document", "restricted": "1"}]
            assert (await itops_assets.module.links.resolve(
                {"x": {"module": "docs", "kind": "document", "id": str(doc_id)}}
            )) == {f"docs:document:{doc_id}": {"text": "Restricted", "href": ""}}  # fmt: skip

        # In the group, and as an admin: everything as before.
        for who in (MEMBER, ADMIN):
            with acting_as(who):
                page = await c.get(f"/docs/d/{slug}")
                assert page.status_code == 200 and f"Isolate first {tag}" in page.text
                assert (await c.get(f"/files/{stored.id}")).status_code == 200
                assert f"Ransomware playbook {tag}" in (await c.get(f"/search?q={tag}")).text
        with acting_as(ADMIN):
            collection_page = (await c.get(f"/docs/c/{col_slug}")).text
            assert "Restricted" in collection_page and GROUP in collection_page
        del fw


async def test_an_area_kept_out_of_ai_is_hidden_from_every_ai_connection(
    database_url: str, tmp_path: Path
) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        with acting_as(ADMIN):
            async with itops_docs.module.session() as s:
                col = await docs.create_collection(s, name=f"Incidents {tag}")
                doc = await docs.create(
                    s, ADMIN, collection_id=col.id, title=f"Post-mortem {tag}",
                    content=Content(blocks=[TextBlock(html="<p>What happened.</p>")]),
                )  # fmt: skip
                await docs.publish(s, ADMIN, doc)
                await s.commit()
                col_id, col_slug, slug = col.id, col.slug, doc.slug
        await wait_for_search(itops_docs.module)
        await _restrict(app, "docs", "collection", str(col_id), groups=[], no_ai=True)

        with acting_as(ADMIN):  # even an admin's AI connection doesn't see it...
            mcp = McpClient(MODULES, app=app)
            assert col_slug not in [x["slug"] for x in await mcp.call("docs_list_collections")]
            with pytest.raises(ToolError, match="No document"):
                await mcp.call("docs_get_document", slug=slug)
            assert not [h for h in await mcp.call("itops_search", query=f"Post-mortem {tag}")
                        if h["title"] == f"Post-mortem {tag}"]  # fmt: skip
            # ...while people, admins and readers, still see it in the browser.
            assert (await c.get(f"/docs/d/{slug}")).status_code == 200
        with acting_as(READER):
            assert (await c.get(f"/docs/d/{slug}")).status_code == 200


async def test_a_restricted_asset_type_is_invisible_outside_its_groups(
    database_url: str, tmp_path: Path
) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        with acting_as(ADMIN):
            async with itops_assets.module.session() as s:
                await s.execute(text("DELETE FROM assets.proposals"))
                cert = await assets.create(s, ADMIN, type_key="ssl_certificate", name=f"vpn-{tag}",
                                           fields={"valid_until": "2030-01-01"})  # fmt: skip
                await s.commit()
            await itops_assets.module.search.query("warm-up")
        await wait_for_search(itops_assets.module)
        await _restrict(app, "assets", "type", "ssl_certificate", groups=[GROUP], no_ai=False)

        with acting_as(READER):
            assert (await c.get(f"/assets/r/{cert.id}")).status_code == 404
            assert (await c.get("/assets/t/ssl_certificate")).status_code == 404
            assert "/assets/t/ssl_certificate" not in (await c.get("/assets/")).text
            mcp = McpClient(MODULES, app=app)
            keys = [t["key"] for t in await mcp.call("itops_list_types", module="assets")]
            assert "ssl_certificate" not in keys and "device" in keys
            with pytest.raises(ToolError, match="No type"):
                await mcp.call("itops_get_type", module="assets", key="ssl_certificate")
            with pytest.raises(ToolError, match="Unknown asset type"):
                await mcp.call("assets_list_records", type="ssl_certificate")
            with pytest.raises(ToolError, match="No asset"):
                await mcp.call("assets_get_record", id=str(cert.id))
            assert not [h for h in await mcp.call("assets_search", query=f"vpn-{tag}")
                        if h["name"] == f"vpn-{tag}"]  # fmt: skip
            with pytest.raises(ToolError, match="isn't an available asset type"):
                await mcp.call("assets_propose_record", type="ssl_certificate", name="x")
        with acting_as(MEMBER):
            assert (await c.get(f"/assets/r/{cert.id}")).status_code == 200
            found = await McpClient(MODULES, app=app).call(
                "assets_list_records", type="ssl_certificate", query=tag
            )
            assert [r["name"] for r in found] == [f"vpn-{tag}"]
        with acting_as(ADMIN):  # the type's page says who sees it, with the way to change it
            page = (await c.get("/assets/types/ssl_certificate")).text
            assert (
                "Who sees these records" in page and "/access/assets/type/ssl_certificate" in page
            )


async def test_admins_change_access_in_the_browser_and_it_is_audited(
    database_url: str, tmp_path: Path
) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        with acting_as(ADMIN):
            async with itops_docs.module.session() as s:
                col = await docs.create_collection(s, name=f"Vendors {tag}")
                await s.commit()
                col_id = col.id
        url = f"/access/docs/collection/{col_id}"
        with acting_as(READER):  # not an admin
            assert (await c.get(url)).status_code == 403
        with acting_as(ADMIN):
            page = (await c.get(url)).text
            assert f"Vendors {tag}" in page and "Keep out of AI connections" in page
            r = await c.post(
                url, data={"groups": f"AD\\{GROUP}\nApp-ITOps-Contracts", "no_ai": "1"}
            )
            assert r.status_code == 303 and r.headers["location"].startswith("/docs/collections/")
            restriction = await app.state.runtime.areas.get("docs", "collection", str(col_id))
            assert restriction.groups == (GROUP, "App-ITOps-Contracts") and restriction.no_ai
            bad = await c.post(url, data={"groups": "bad*group"})
            assert bad.status_code == 303 and "error=" in bad.headers["location"]
            assert "kept=" in bad.headers["location"]  # the form comes back as typed
            await c.post(url, data={"groups": ""})  # open it again
            assert await app.state.runtime.areas.get("docs", "collection", str(col_id)) is None
            assert (await c.get("/access/docs/collection/999999")).status_code == 404
            assert (await c.get("/access/docs/nonsense/1")).status_code == 404
        with acting_as(replace(ADMIN, via_ai=True)):
            assert (await c.get(url)).status_code == 403  # never through an AI connection
        async with app.state.runtime.sessionmaker() as s:
            actions = [
                r[0]
                for r in await s.execute(
                    text("SELECT action FROM itops.audit_log WHERE entity_id = :e ORDER BY id"),
                    {"e": f"docs:collection:{col_id}"},
                )
            ]
        assert actions == ["area.restricted", "area.opened"]


async def test_editing_keeps_links_the_editor_cannot_see(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        with acting_as(ADMIN):
            async with itops_assets.module.session() as s:
                hidden = await assets.create(
                    s, ADMIN, type_key="ssl_certificate", name=f"secret-{tag}",
                    fields={"valid_until": "2030-01-01"},
                )  # fmt: skip
                await s.commit()
            async with itops_docs.module.session() as s:
                doc = await docs.create(
                    s, ADMIN, collection_id=1, title=f"Cert note {tag}", content=Content(),
                    type_key="security_note",
                    fields={"device": f"assets:ssl_certificate:{hidden.id}"},
                )  # fmt: skip
                await s.commit()
                slug = doc.slug
        await _restrict(app, "assets", "type", "ssl_certificate", groups=[GROUP], no_ai=False)
        with acting_as(READER):
            form = (await c.get(f"/docs/d/{slug}/edit")).text
            # The current value stays selected (labelled Restricted), so saving keeps it.
            assert f'value="assets:ssl_certificate:{hidden.id}" selected' in form
            assert f"secret-{tag}" not in form
