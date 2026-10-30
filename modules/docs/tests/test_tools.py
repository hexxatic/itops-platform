"""Docs MCP tools, called through the real MCP server (so they run *via AI*)."""

from __future__ import annotations

import base64
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from itops_docs import module
from sqlalchemy import text

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
            "docs_upload_image",
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
        assert made["review_url"] == made["url"] and "You can't approve it" in made["next"]

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
        await web.post(
            f"/api/docs/documents/{made['slug']}/publish?version=1"
        )  # a person publishes

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

        # The edit is listed as waiting, with its note; the published listing flags it.
        pending = await mcp.call("docs_list_documents", status="pending")
        [waiting] = [d for d in pending if d["slug"] == made["slug"]]
        assert waiting["has_unpublished_changes"]
        assert waiting["pending_version"] == 2
        assert waiting["change_note"] == "Rotation is now weekly"
        assert waiting["saved_by"].endswith("(via AI)")
        published = await mcp.call("docs_list_documents", status="published")
        assert [d["has_unpublished_changes"] for d in published if d["slug"] == made["slug"]] == [
            True
        ]


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


# The smallest valid PNG (1x1, transparent): enough for the type check by contents.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


async def test_ai_uploads_a_screenshot_for_a_draft(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (mcp, web):
        up = await mcp.call(
            "docs_upload_image", filename="C:\\shots\\send button.png",
            content_base64=base64.b64encode(PNG).decode(), alt="The [Send] button",
        )  # fmt: skip
        assert up["type"] == "image/png" and up["size"] == len(PNG)
        assert up["url"] == f"/files/{up['id']}"
        assert up["markdown"] == f"![The (Send) button]({up['url']})"
        made = await mcp.call(
            "docs_create_document", title="Send the sheets", collection="procedures",
            markdown=f"Click Send.\n\n{up['markdown']}",
        )  # fmt: skip
        assert made["status"] == "draft"
        page = (await web.get(made["url"] + "?draft=1")).text
        assert f'src="{up["url"]}"' in page
        assert (await web.get(up["url"])).content == PNG
        async with module.session() as s:
            row = (
                await s.execute(
                    text("SELECT entity_type, filename FROM itops.files WHERE id = :i"),
                    {"i": up["id"]},
                )
            ).one()
        assert tuple(row) == ("document", "send button.png")  # claimed by the draft


async def test_upload_image_takes_images_only(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (mcp, _):
        fake = base64.b64encode(b"%PDF-1.7 not a picture").decode()
        with pytest.raises(ToolError, match="isn't a PNG, JPEG, GIF or WebP"):
            await mcp.call("docs_upload_image", filename="shot.png", content_base64=fake)
        with pytest.raises(ToolError, match="isn't valid base64"):
            await mcp.call("docs_upload_image", filename="shot.png", content_base64="%%%")
        svg = base64.b64encode(b"<svg onload='alert(1)'/>").decode()
        with pytest.raises(ToolError, match=re.escape("isn't a PNG")):
            await mcp.call("docs_upload_image", filename="shot.svg", content_base64=svg)
