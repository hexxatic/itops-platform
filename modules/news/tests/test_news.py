"""News: drafts, publishing, pinning, the feed, search, and what AI may (not) do."""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from itops_news import module
from itops_news import service as news
from sqlalchemy import text

from itops.sdk import Principal
from itops.sdk.content import Content, TextBlock
from itops.testing import assert_conformant, assert_migrations_roundtrip
from itops.testing.app import McpClient, ToolError, acting_as, make_app, wait_for_search

ADMIN = Principal("admin@example.com", "Admin", frozenset({"*"}))
WRITER = Principal("writer@example.com", "Writer", frozenset({"news.read", "news.write"}))
READER = Principal("reader@example.com", "Reader", frozenset({"news.read"}))


def body(words: str) -> Content:
    return Content(blocks=[TextBlock(html=f"<p>{words}</p>")])


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM news.posts"))
            await s.execute(text("DELETE FROM itops.search_passages WHERE module = 'news'"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


async def button(c: httpx.AsyncClient, slug: str, action: str) -> str:
    """Where the post page's button for ``action`` posts (with the stamp it shows)."""
    page = (await c.get(f"/news/p/{slug}")).text
    m = re.search(rf'action="(/api/news/posts/{slug}/{action}\?[^"]*)"', page)
    assert m, action
    return html.unescape(m[1])


def test_conformance() -> None:
    assert_conformant(module)


async def test_migrations_roundtrip(database_url: str) -> None:
    await assert_migrations_roundtrip(module, database_url)


async def test_a_post_is_a_draft_until_a_person_publishes_it(
    database_url: str, tmp_path: Path
) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as c:
        with acting_as(WRITER):
            r = await c.post("/api/news/posts", data={
                "title": f"Printers down Friday {tag}", "category": "it",
                "content": body("Maintenance from 6 to 8.").model_dump_json()})  # fmt: skip
            assert r.status_code == 303
            slug = r.headers["location"].rsplit("/", 1)[-1]
            assert f"Printers down Friday {tag}" in (await c.get("/news/")).text  # their drafts
            # A writer can't publish.
            assert (await c.post(f"/api/news/posts/{slug}/publish")).status_code == 403
        with acting_as(READER):  # readers don't see drafts at all
            assert f"Printers down Friday {tag}" not in (await c.get("/news/")).text
            assert (await c.get(f"/news/p/{slug}")).status_code == 404
        with acting_as(ADMIN):
            assert (await c.post(await button(c, slug, "publish"))).status_code == 303
        await wait_for_search(module)
        with acting_as(READER):
            page = (await c.get("/news/")).text
            assert f"Printers down Friday {tag}" in page and "Maintenance from 6 to 8." in page
            assert "IT announcement" in page
            assert f"Printers down Friday {tag}" not in (await c.get("/news/?category=app")).text
            hits = await module.search.query(f"Printers {tag}")
            assert [h.title for h in hits] == [f"Printers down Friday {tag}"]
            # Published: only someone who can publish changes it.
        with acting_as(WRITER):
            r = await c.post(f"/api/news/posts/{slug}/save", data={
                "title": "Changed", "category": "it",
                "content": body("x").model_dump_json()})  # fmt: skip
            assert "error=" in r.headers["location"]


async def test_pinned_posts_come_first_and_archived_ones_leave_the_feed(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        with acting_as(ADMIN):
            async with module.session() as s:
                older = await news.create(s, ADMIN, title="Older", category="app",
                                          content=body("one"))  # fmt: skip
                await news.publish(s, ADMIN, older, reviewed=None)
                newer = await news.create(s, ADMIN, title="Newer", category="app",
                                          content=body("two"))  # fmt: skip
                await news.publish(s, ADMIN, newer, reviewed=None)
                await s.commit()
                assert [p.title for p in await news.feed(s)] == ["Newer", "Older"]
            await c.post(f"/api/news/posts/{older.slug}/pin")
            async with module.session() as s:
                assert [p.title for p in await news.feed(s)] == ["Older", "Newer"]
            await c.post(f"/api/news/posts/{older.slug}/archive")
            async with module.session() as s:
                assert [p.title for p in await news.feed(s)] == ["Newer"]
            assert "Older" in (await c.get("/news/archive")).text
            await c.post(f"/api/news/posts/{older.slug}/restore")
            async with module.session() as s:
                restored = await news.by_slug(s, older.slug)
                assert restored is not None and restored.status == "published"
                assert not restored.pinned  # archiving unpinned it


async def test_ai_drafts_news_but_never_publishes_or_changes_published_posts(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path):
        mcp = McpClient([module])
        with acting_as(ADMIN):  # even an admin's AI connection
            made = await mcp.call(
                "news_draft_post", title="News is live", category="app",
                markdown="IT-Ops now has a **News** page.",
            )  # fmt: skip
            assert made["status"] == "draft" and made["url"].endswith("/news/p/news-is-live")
            assert await mcp.call("news_list_posts") == []  # drafts aren't news yet
            changed = await mcp.call("news_update_draft", slug="news-is-live",
                                     markdown="IT-Ops now has a News page. Pin it.")  # fmt: skip
            assert changed["status"] == "draft"
            async with module.session() as s:
                post = await news.by_slug(s, "news-is-live")
                assert post is not None and post.created_via_ai
                await news.publish(s, ADMIN, post, reviewed=None)  # a person publishes
                await s.commit()
            listed = await mcp.call("news_list_posts", category="app")
            assert [p["title"] for p in listed] == ["News is live"]
            assert "Pin it." in (await mcp.call("news_get_post", slug="news-is-live"))["text"]
            with pytest.raises(ToolError, match="already published"):
                await mcp.call("news_update_draft", slug="news-is-live", markdown="Sneaky edit")
            with pytest.raises(ToolError, match="'app'"):
                await mcp.call("news_draft_post", title="x", category="sports", markdown="x")
        assert sorted(await mcp.tool_names()) == [
            "news_draft_post", "news_get_post", "news_list_posts", "news_update_draft",
        ]  # fmt: skip


async def test_publish_and_discard_refuse_a_draft_changed_after_the_page_loaded(
    database_url: str, tmp_path: Path
) -> None:
    """An AI connection can change a draft while a person has it open: Publish then
    refuses, so nothing goes out unread."""
    async with running(database_url, tmp_path) as c:
        mcp = McpClient([module])
        with acting_as(ADMIN):
            await mcp.call("news_draft_post", title="Patch night", category="it",
                           markdown="Servers restart at 22:00.")  # fmt: skip
            publish = await button(c, "patch-night", "publish")
            discard = await button(c, "patch-night", "delete")
            await mcp.call("news_update_draft", slug="patch-night",
                           markdown="Servers restart at 20:00.")  # fmt: skip
            for url in (publish, discard):
                r = await c.post(url, follow_redirects=False)
                assert r.headers["location"].startswith("/news/p/patch-night?error=Not%20")
                assert "after you opened it" in unquote(r.headers["location"])
            async with module.session() as s:
                post = await news.by_slug(s, "patch-night")
                assert post is not None and post.status == "draft"
            # Reloaded, the page shows the change and publishes it.
            r = await c.post(await button(c, "patch-night", "publish"), follow_redirects=False)
            assert r.headers["location"] == "/news/p/patch-night"
            got = await mcp.call("news_get_post", slug="patch-night")
            assert got["status"] == "published" and "20:00" in got["text"]
            # The stamp is required: it's how the page says what the person saw.
            r = await c.post("/api/news/posts/patch-night/publish", follow_redirects=False)
            assert r.status_code == 422
