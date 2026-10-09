"""Search, end to end: publishing indexes, drafts never show up, archived docs only when
asked for, results are permission-filtered and cite the right section. Runs with Full-Text on dev
boxes and with the plain-matching fallback in CI."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from itops_docs import module

from itops.sdk import Principal
from itops.sdk.content import Content, HeadingBlock, StepBlock, TextBlock
from itops.testing.app import McpClient, acting_as, clear_search, make_app, wait_for_search


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, McpClient]]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as web:
            await clear_search(module)
            yield web, McpClient([module], app=app)


async def publish(web: httpx.AsyncClient, title: str, *blocks: object, publish: bool = True) -> str:
    content = Content(blocks=list(blocks)).model_dump_json()  # type: ignore[arg-type]
    r = await web.post(
        "/api/docs/documents",
        data={"title": title, "collection_id": "1", "content": content},
    )
    slug = r.headers["location"].split("/docs/d/")[1].split("?")[0]
    if publish:
        await web.post(f"/api/docs/documents/{slug}/publish")
    await wait_for_search(module)
    return slug


FAILOVER = (
    HeadingBlock(text="Failover"),
    StepBlock(html="<p>Stop the primary node</p>"),
    StepBlock(html="<p>Promote the replica on <code>SQL-DR1</code></p>"),
)


async def test_published_docs_are_found_and_cited(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, _):
        slug = await publish(web, "Database failover runbook", *FAILOVER)
        hits = await module.search.query("promote replica")
        assert [h.title for h in hits] == ["Database failover runbook"]
        assert hits[0].label == "Failover > Step 2"
        assert hits[0].url == f"/docs/d/{slug}#failover"
        assert "<mark>" in hits[0].snippet


async def test_drafts_are_never_searchable(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, _):
        slug = await publish(
            web, "Secret plan", TextBlock(html="<p>zebra unicorn</p>"), publish=False
        )
        assert await module.search.query("zebra") == []
        # Publishing makes it searchable; a newer draft doesn't change what's found.
        await web.post(f"/api/docs/documents/{slug}/publish")
        await web.post(
            f"/api/docs/documents/{slug}/save",
            data={"content": Content(blocks=[TextBlock(html="<p>giraffe</p>")]).model_dump_json()},
        )
        await wait_for_search(module)
        assert [h.title for h in await module.search.query("zebra")] == ["Secret plan"]
        assert await module.search.query("giraffe") == []


async def test_archived_is_hidden_unless_asked_for(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, mcp):
        slug = await publish(web, "Legacy VPN", TextBlock(html="<p>pptp tunnel</p>"))
        await web.post(f"/api/docs/documents/{slug}/archive")
        await wait_for_search(module)
        assert await module.search.query("pptp") == []
        [hit] = await module.search.query("pptp", include_archived=True)
        assert hit.archived

        # The "Include archived" checkbox on both search pages, with the badge.
        for page in ("/docs/search", "/search"):
            plain = (await web.get(page, params={"q": "pptp"})).text
            assert "Legacy VPN" not in plain and 'name="archived"' in plain
            with_archived = (await web.get(page, params={"q": "pptp", "archived": "1"})).text
            assert "Legacy VPN" in with_archived and ">Archived<" in with_archived

        # The AI tools take the same option.
        assert await mcp.call("docs_search", query="pptp") == []
        [ai_hit] = await mcp.call("itops_search", query="pptp", include_archived=True)
        assert ai_hit["archived"] is True

        await web.post(f"/api/docs/documents/{slug}/restore")
        await wait_for_search(module)
        [restored] = await module.search.query("pptp")
        assert not restored.archived


async def test_collection_page_show_archived_toggle(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, _):
        slug = await publish(web, "Old fax server", TextBlock(html="<p>fax</p>"))
        await web.post(f"/api/docs/documents/{slug}/archive")
        listing = (await web.get("/docs/c/general")).text
        assert "Old fax server" not in listing and "Show archived" in listing
        shown = (await web.get("/docs/c/general", params={"archived": "1"})).text
        assert "Old fax server" in shown and "Archived" in shown


async def test_rename_updates_the_link(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, _):
        slug = await publish(web, "Printer notes", TextBlock(html="<p>toner codes</p>"))
        r = await web.post(
            f"/api/docs/documents/{slug}/details",
            data={"title": "Printer fleet", "collection_id": "1", "tags": ""},
        )
        new_url = r.headers["location"]  # e.g. /docs/d/printer-fleet (or -2 if taken)
        assert new_url != f"/docs/d/{slug}"
        await wait_for_search(module)
        [hit] = await module.search.query("toner")
        assert hit.title == "Printer fleet" and hit.url == new_url


async def test_results_respect_read_permission(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, _):
        await publish(web, "Router config", TextBlock(html="<p>ospf area zero</p>"))
        nobody = Principal(user="eve", display_name="Eve", permissions=frozenset({"other.read"}))
        reader = Principal(user="bob", display_name="Bob", permissions=frozenset({"docs.read"}))
        with acting_as(nobody):
            assert await module.search.query("ospf") == []
        with acting_as(reader):
            assert len(await module.search.query("ospf")) == 1


async def test_search_pages(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, _):
        await publish(web, "Backup rotation", TextBlock(html="<p>tapes go offsite weekly</p>"))
        docs_page = (await web.get("/docs/search", params={"q": "offsite"})).text
        assert "Backup rotation" in docs_page and "1 result" in docs_page
        everything = (await web.get("/search", params={"q": "offsite"})).text
        assert "Backup rotation" in everything and "badge" in everything  # module badge
        assert "No results" in (await web.get("/search", params={"q": "nothinghere"})).text
        home = (await web.get("/docs/")).text
        assert 'action="/search"' in home  # top-bar search box
        assert 'name="archived"' in home  # page box: "Include archived" before the first search
        # Search pages update as you type (htmx swaps just the results block).
        assert 'hx-get="/docs/search"' in docs_page and 'id="search-results"' in docs_page
        assert 'hx-get="/search"' in everything


async def test_search_tools_for_ai(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (web, mcp):
        slug = await publish(web, "Database failover runbook", *FAILOVER)
        [hit] = await mcp.call("docs_search", query="promote replica")
        assert hit["slug"] == slug and hit["where"] == "Failover > Step 2"
        assert hit["url"].startswith("http") and "<mark>" not in hit["snippet"]
        everywhere = await mcp.call("itops_search", query="promote replica")
        assert [h["module"] for h in everywhere] == ["docs"]


PRINTER = TextBlock(
    html="<p>Clearing stuck jobs: restart the Print Spooler service on the print server "
    "and purge the spool folder.</p>"
)


async def test_semantic_search_finds_meaning_without_shared_words(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (web, _):
        if not module.ai.embeddings_available:
            pytest.skip("local embedding model not available")
        await publish(web, "Printer maintenance", PRINTER)
        # No word in common with the document: only the meaning connects them.
        [hit] = await module.search.query("printouts are not coming out")
        assert hit.title == "Printer maintenance" and hit.match == "meaning"
        page = await web.get("/docs/search", params={"q": "printouts are not coming out"})
        assert "Related" in page.text
        # Off-topic questions don't drag in random documents.
        assert await module.search.query("banana smoothie recipe") == []


async def test_keyword_and_meaning_agree_on_a_direct_match(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (web, _):
        if not module.ai.embeddings_available:
            pytest.skip("local embedding model not available")
        await publish(web, "Printer maintenance", PRINTER)
        [hit] = await module.search.query("restart print spooler")
        assert hit.match == "both"


async def test_typos_are_corrected_against_our_own_vocabulary(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (web, mcp):
        await publish(
            web, "Gainseeker on RDS", TextBlock(html="<p>Restart the Gainseeker client.</p>")
        )
        [hit] = await module.search.query("gainseeker client")
        assert hit.title == "Gainseeker on RDS" and hit.corrected == ""  # spelt right: no flag
        [hit] = await module.search.query("gainseker")
        assert hit.corrected == "gainseeker" and "<mark>Gainseeker</mark>" in hit.snippet
        # As typed, only meaning can find it (the embedding is a little typo-tolerant).
        as_typed = await module.search.query("gainseker", fuzzy=False)
        assert all(h.corrected == "" and h.match == "meaning" for h in as_typed)
        page = (await web.get("/docs/search", params={"q": "gainseker"})).text
        assert "Also showing results for <strong>gainseeker</strong>" in page
        assert "exact=1" in page and "Spelling" in page
        exact = (await web.get("/docs/search", params={"q": "gainseker", "exact": "1"})).text
        assert "Also showing results for" not in exact
        # AI tools get the same correction, and are told about it.
        found = await mcp.call("docs_search", query="gainseker")
        assert "corrected_query" in str(found) and "Gainseeker on RDS" in str(found)
