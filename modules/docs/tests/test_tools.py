"""Docs MCP tools, called through the real MCP server (so they run *via AI*)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from itops_docs import module

from itops.testing.app import McpClient, ToolError, make_app


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[McpClient, httpx.AsyncClient]]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as web:
            yield McpClient([module]), web


RUNBOOK = """Restart the print spooler when jobs get stuck.

## Steps

1. Open Services
2. Restart **Print Spooler**
"""


async def test_tools_are_registered(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (mcp, _):
        assert sorted(await mcp.tool_names()) == [
            "docs_create_document",
            "docs_get_document",
            "docs_list_collections",
            "docs_list_documents",
            "docs_search",
            "docs_update_document",
        ]
        cols = await mcp.call("docs_list_collections")
        assert {c["slug"] for c in cols} >= {"general", "procedures"}


async def test_ai_creates_a_draft_it_cannot_publish(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (mcp, web):
        made = await mcp.call(
            "docs_create_document",
            title="Print spooler restart",
            collection="procedures",
            markdown=RUNBOOK,
            tags=["printing"],
        )
        assert made["status"] == "draft"
        assert made["url"].endswith("/docs/d/print-spooler-restart")
        assert "must review and publish" in made["next"]

        doc = await mcp.call("docs_get_document", slug=made["slug"], include_draft=True)
        assert [b["type"] for b in doc["blocks"]] == ["text", "heading", "text"]
        assert "## Steps" in doc["text"] and "Print Spooler" in doc["text"]
        assert doc["tags"] == ["printing"]

        # There is no publish tool, and the history records the change as made via AI.
        assert not [n for n in await mcp.tool_names() if "publish" in n]
        page = (await web.get(f"/docs/d/{made['slug']}")).text
        assert "dev (via AI)" in page


async def test_update_saves_a_new_draft_and_readers_keep_the_published(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (mcp, web):
        made = await mcp.call(
            "docs_create_document", title="Wifi guest network", collection="networking",
            markdown="Password rotates monthly.",
        )  # fmt: skip
        await web.post(f"/api/docs/documents/{made['slug']}/publish")  # a person publishes

        upd = await mcp.call(
            "docs_update_document",
            slug=made["slug"],
            change_note="Rotation is now weekly",
            blocks=[{"type": "text", "html": "<p>Password rotates weekly.</p>"}],
        )
        assert upd["version"] == 2

        published = await mcp.call("docs_get_document", slug=made["slug"])
        assert "monthly" in published["text"] and published["has_unpublished_changes"]
        draft = await mcp.call("docs_get_document", slug=made["slug"], include_draft=True)
        assert "weekly" in draft["text"] and draft["showing"] == "draft"


async def test_html_content_is_kept_but_sanitized(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (mcp, _):
        made = await mcp.call(
            "docs_create_document",
            title="Rich runbook",
            collection="general",
            html="<details><summary>More</summary><p>x</p></details><script>bad()</script>",
        )
        doc = await mcp.call("docs_get_document", slug=made["slug"], include_draft=True)
        html = doc["blocks"][0]["html"]
        assert doc["blocks"][0]["type"] == "html"
        assert "<details>" in html and "script" not in html


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"title": "X", "collection": "nope", "markdown": "a"}, "Unknown collection"),
        ({"title": "X", "collection": "general"}, "exactly one of"),
        (
            {"title": "X", "collection": "general", "markdown": "a", "html": "<p>b</p>"},
            "exactly one of",
        ),
        ({"title": "X", "collection": "general", "blocks": [{"type": "video"}]}, "Invalid blocks"),
    ],
)
async def test_helpful_errors(
    database_url: str, tmp_path: Path, args: dict[str, object], message: str
) -> None:
    async with running(database_url, tmp_path) as (mcp, _):
        with pytest.raises(ToolError, match=message):
            await mcp.call("docs_create_document", **args)
