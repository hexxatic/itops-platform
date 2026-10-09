"""Docs and assets link to each other (ADR 0008/0009) through services, both ways.

Integration tests load several modules together, so they live outside any one module
(module tests may only import their own module)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import itops_assets
import itops_docs
from itops_assets import service as assets
from itops_docs import service as docs
from sqlalchemy import text

from itops.sdk import Principal
from itops.sdk.content import Content, TextBlock
from itops.testing.app import McpClient, ToolError, make_app

ME = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
MODULES = [itops_docs.module, itops_assets.module]

APPLICATION: dict[str, Any] = {
    "name": "Application",
    "fields": [
        {"key": "servers", "label": "Runs on", "kind": "link", "target": "assets.device",
         "multiple": True},
        {"key": "vendor_url", "label": "Vendor site", "kind": "url"},
    ],
}  # fmt: skip


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, Any]]:
    app = make_app(database_url, MODULES, files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with runtime.sessionmaker() as s:
            await s.execute(text("DELETE FROM assets.proposals"))
            await s.execute(text("DELETE FROM assets.records"))
            if await runtime.types.get(s, "docs", "application") is None:
                await runtime.types.draft(s, "docs", APPLICATION)
                await runtime.types.publish(s, "docs", "application")
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c, app


async def test_documents_and_assets_link_both_ways(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        async with itops_assets.module.session() as s:
            sql = await assets.create(s, ME, type_key="device", name=f"SQL-{tag}")
            await s.commit()
        device_link = f"assets:device:{sql.id}"

        # The document form offers devices in the "Runs on" picker.
        form = (await c.get("/docs/new?collection=1&type=application")).text
        assert f'value="{device_link}"' in form and f"SQL-{tag}" in form

        body = Content(blocks=[TextBlock(html="<p>Payroll app.</p>")]).model_dump_json()
        r = await c.post("/api/docs/documents", data={
            "title": f"Payroll {tag}", "collection_id": "1", "content": body,
            "type_key": "application", "f__servers": device_link})  # fmt: skip
        m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
        assert m, r.headers["location"]
        slug = m[1]
        await c.post(f"/api/docs/documents/{slug}/publish")

        # The document shows the device by name; the device lists the document.
        page = (await c.get(f"/docs/d/{slug}")).text
        assert f'href="/assets/r/{sql.id}"' in page and f"SQL-{tag}" in page
        record = (await c.get(f"/assets/r/{sql.id}")).text
        assert "Documents" in record and f"Payroll {tag}" in record and "Runs on" in record

        # Assets can link to documents too, and to each other ("Linked from").
        async with itops_docs.module.session() as s:
            doc = await docs.by_slug(s, slug)
            assert doc is not None
            doc_id = doc.id
        runtime = app.state.runtime
        async with runtime.sessionmaker() as s:
            ip = await runtime.types.get(s, "assets", "ip_address")
            changed = ip.published.definition.model_dump()
            if "runbook" not in {f["key"] for f in changed["fields"]}:
                changed["fields"].append(
                    {"key": "runbook", "label": "Runbook", "kind": "link", "target": "docs"}
                )
                await runtime.types.draft(s, "assets", changed, key="ip_address")
                await runtime.types.publish(s, "assets", "ip_address")
                await s.commit()
        form = (await c.get("/assets/t/ip_address/new")).text
        assert f'value="docs:document:{doc_id}"' in form
        r = await c.post("/api/assets/records", data={
            "type_key": "ip_address", "name": f"10.9.0.{len(tag)}",
            "f__switch": device_link, "f__runbook": f"docs:document:{doc_id}"})  # fmt: skip
        ip_id = r.headers["location"].rsplit("/", 1)[-1]
        assert f"Payroll {tag}" in (await c.get(f"/assets/r/{ip_id}")).text
        record = (await c.get(f"/assets/r/{sql.id}")).text
        assert "Linked from" in record and "10.9.0." in record
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "10.9.0." in page and "Runbook" in page  # the document lists the asset

        # The type editor offers both modules' link targets.
        editor = (await c.get("/docs/types/new")).text
        assert "Assets: Device" in editor and "Docs: a document" in editor


async def test_ai_tools_read_and_propose(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, _):
        mcp = McpClient(MODULES)
        assert {n for n in await mcp.tool_names() if n.startswith("assets_")} == {
            "assets_get_record", "assets_list_proposals", "assets_list_records",
            "assets_propose_change", "assets_propose_record", "assets_search"}  # fmt: skip
        async with itops_assets.module.session() as s:
            hq = await assets.create(s, ME, type_key="location", name=f"HQ {tag}")
            sql = await assets.create(s, ME, type_key="device", name=f"SQL-{tag}", fields={
                "kind": "Server", "primary_ip": "10.0.1.15",
                "location": f"assets:location:{hq.id}"})  # fmt: skip
            await s.commit()

        found = await mcp.call("assets_list_records", type="device", query="10.0.1.15")
        assert [f["name"] for f in found] == [f"SQL-{tag}"]
        with_type = await mcp.call("assets_get_record", id=str(sql.id))
        assert with_type["fields"]["location"] == {
            "link": f"assets:location:{hq.id}", "name": f"HQ {tag}"}  # fmt: skip
        try:
            await mcp.call("assets_list_records", type="spaceship")
            raise AssertionError("expected a ToolError")
        except ToolError as exc:
            assert "Unknown asset type" in str(exc)

        # AI proposes; nothing changes until a person approves.
        new = await mcp.call("assets_propose_record", type="domain", name=f"{tag}.example.com",
                             fields={"expires": "2027-01-15"}, note="from WHOIS")  # fmt: skip
        assert new["status"] == "pending" and new["review_url"].endswith("/assets/proposals")
        change = await mcp.call("assets_propose_change", id=str(sql.id), note="new address",
                                fields={"primary_ip": "10.0.1.16", "location": None})  # fmt: skip
        assert change["fields"] == {"kind": "Server", "primary_ip": "10.0.1.16"}
        assert len(await mcp.call("assets_list_proposals")) == 2
        assert (await mcp.call("assets_get_record", id=str(sql.id)))["pending_proposals"]
        try:
            await mcp.call("assets_propose_record", type="ssl_certificate", name="x.example.com")
            raise AssertionError("expected a ToolError")
        except ToolError as exc:
            assert "Valid until is required" in str(exc)

        async with itops_assets.module.session() as s:
            current = await assets.get(s, sql.id)
            assert current and assets.record_fields(current)["primary_ip"] == "10.0.1.15"
        r = await c.post(f"/api/assets/proposals/{change['proposal']}/approve")
        assert r.headers["location"] == "/assets/proposals"
        async with itops_assets.module.session() as s:
            current = await assets.get(s, sql.id)
            assert current and assets.record_fields(current) == {
                "kind": "Server", "primary_ip": "10.0.1.16"}  # fmt: skip
