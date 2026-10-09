"""Docs module: conformance, migrations, and the document lifecycle end to end."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from itops_docs import module, service
from itops_docs.service import DocsError

from itops.sdk import Principal
from itops.sdk.content import Content, HeadingBlock, StepBlock, TextBlock
from itops.testing import assert_conformant, assert_migrations_roundtrip
from itops.testing.app import McpClient, make_app

HUMAN = Principal(user="alice", display_name="Alice", permissions=frozenset({"*"}))
AI = Principal(user="alice", display_name="Alice", permissions=frozenset({"*"}), via_ai=True,
               client="mcp")  # fmt: skip


def body(*blocks: object) -> str:
    return Content(blocks=list(blocks)).model_dump_json()  # type: ignore[arg-type]


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


async def new_doc(c: httpx.AsyncClient, title: str, *blocks: object) -> str:
    r = await c.post(
        "/api/docs/documents",
        data={"title": title, "collection_id": "1", "content": body(*blocks), "tags": "a, B"},
    )
    assert r.status_code == 303, r.text
    m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
    assert m, r.headers["location"]
    return m[1]


def test_conformance() -> None:
    assert_conformant(module)


async def test_migrations_roundtrip(database_url: str) -> None:
    await assert_migrations_roundtrip(module, database_url)


async def test_create_is_a_draft_until_published(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(
            c, "VLAN setup", HeadingBlock(text="Steps"), StepBlock(html="<p>Plug in</p>")
        )
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "looking at the draft" in page
        assert "Plug in" in page
        assert "Drafts waiting to be published" in (await c.get("/docs/")).text

        assert (await c.post(f"/api/docs/documents/{slug}/publish")).status_code == 303
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "looking at the draft" not in page
        assert "Published" in page
        assert "Review by" in page  # next review date set on publish


async def test_edit_keeps_readers_on_the_published_version(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(c, "Backup policy", TextBlock(html="<p>Version one</p>"))
        await c.post(f"/api/docs/documents/{slug}/publish")
        r = await c.post(
            f"/api/docs/documents/{slug}/save",
            data={"content": body(TextBlock(html="<p>Version two</p>")), "change_note": "v2"},
        )
        assert r.status_code == 303
        reader = (await c.get(f"/docs/d/{slug}")).text
        assert "Version one" in reader and "Version two" not in reader
        assert "unpublished changes" in reader
        assert "Version two" in (await c.get(f"/docs/d/{slug}?draft=1")).text
        assert "Version two" in (await c.get(f"/docs/d/{slug}/edit")).text

        await c.post(f"/api/docs/documents/{slug}/discard")  # drop the draft
        reader = (await c.get(f"/docs/d/{slug}")).text
        assert "Version one" in reader and "unpublished changes" not in reader


async def test_discarding_a_never_published_doc_deletes_it(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(c, "Scratch", TextBlock(html="<p>temp</p>"))
        r = await c.post(f"/api/docs/documents/{slug}/discard")
        assert r.headers["location"] == "/docs/"
        assert (await c.get(f"/docs/d/{slug}")).status_code == 404


async def test_archive_and_restore(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(c, "Old VPN", TextBlock(html="<p>legacy</p>"))
        await c.post(f"/api/docs/documents/{slug}/publish")
        await c.post(f"/api/docs/documents/{slug}/archive")
        assert "Archived" in (await c.get(f"/docs/d/{slug}")).text
        assert "Old VPN" not in (await c.get("/docs/c/general")).text
        await c.post(f"/api/docs/documents/{slug}/restore")
        assert "Old VPN" in (await c.get("/docs/c/general")).text


async def test_scripts_never_reach_storage(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(
            c, "Sneaky", TextBlock(html='<p onclick="x()">ok</p><script>alert(1)</script>')
        )
        async with module.session() as s:
            doc = await service.by_slug(s, slug)
            assert doc is not None
            stored = (await service.versions(s, doc.id))[0].content
        assert "script" not in stored and "onclick" not in stored
        assert json.loads(stored)["blocks"][0]["html"] == "<p>ok</p>"


async def test_ai_can_draft_but_never_publish(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(c, "AI runbook", TextBlock(html="<p>draft</p>"))
        async with module.session() as s:
            doc = await service.by_slug(s, slug)
            assert doc is not None
            await service.save(s, AI, doc, Content(blocks=[TextBlock(html="<p>ai edit</p>")]))
            for action in (service.publish, service.discard, service.archive):
                with pytest.raises(DocsError, match="done by a person"):
                    await action(s, AI, doc)
            await s.commit()
            history = await service.versions(s, doc.id)
        assert history[0].author_via_ai and history[0].author_client == "mcp"


async def test_details_edit_renames_and_retags(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(c, "Printer notes", TextBlock(html="<p>x</p>"))
        r = await c.post(
            f"/api/docs/documents/{slug}/details",
            data={"title": "Printer fleet notes", "collection_id": "2", "tags": "printers"},
        )
        assert r.headers["location"] == "/docs/d/printer-fleet-notes"
        page = (await c.get("/docs/d/printer-fleet-notes")).text
        assert "printers" in page and "Procedures" in page


async def test_empty_document_is_refused(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        r = await c.post(
            "/api/docs/documents",
            data={"title": "Empty", "collection_id": "1", "content": body(TextBlock(html=" "))},
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/docs/new?")
        assert "empty" in (await c.get(r.headers["location"])).text


@pytest.mark.parametrize("path", ["/docs/", "/docs/new", "/docs/c/general"])
async def test_pages_render(database_url: str, tmp_path: Path, path: str) -> None:
    async with running(database_url, tmp_path) as c:
        r = await c.get(path)
        assert r.status_code == 200
        assert 'class="shell-header"' in r.text


async def test_editor_page_loads_the_island(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        page = (await c.get("/docs/new")).text
        assert "data-section-editor" in page
        assert "islands/section-editor.js" in page
        assert (await c.get("/static/islands/section-editor.js")).status_code == 200


# ---- audit trail (ADR 0006) ------------------------------------------------------


async def actions(slug: str) -> list[tuple[str, bool]]:
    async with module.session() as s:
        doc = await service.by_slug(s, slug)
        assert doc is not None
    entries = await module.audit.history(kind="document", entity_id=str(doc.id))
    return [(e.action, e.via_ai) for e in entries]


async def test_lifecycle_is_audited(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(c, "Audit me", TextBlock(html="<p>one</p>"))
        await c.post(f"/api/docs/documents/{slug}/publish")
        await c.post(
            f"/api/docs/documents/{slug}/save",
            data={"content": body(TextBlock(html="<p>two</p>")), "change_note": "typo"},
        )
        await c.post(f"/api/docs/documents/{slug}/discard")
        await c.post(f"/api/docs/documents/{slug}/archive")
        await c.post(f"/api/docs/documents/{slug}/restore")

        assert [a for a, _ in await actions(slug)] == [
            "document.restored",
            "document.archived",
            "document.draft_discarded",
            "document.edited",
            "document.published",
            "document.created",
        ]
        page = (await c.get(f"/docs/d/{slug}")).text
        assert "Activity (6)" in page and "Saved draft version 2: typo" in page

        audit = (await c.get("/audit?module=docs")).text
        assert "Audit me" in audit and "Published version 1" in audit


async def test_ai_writes_are_marked_via_ai(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        made = await McpClient([module]).call(
            "docs_create_document", title="Drafted by AI", collection="general", markdown="Hi"
        )
        slug = made["url"].rsplit("/", 1)[-1]
        assert await actions(slug) == [("document.created", True)]
        assert "via AI" in (await c.get("/audit?ai=1")).text


async def test_ai_cannot_change_a_published_documents_details(
    database_url: str, tmp_path: Path
) -> None:
    """Details aren't drafted (they go live at once), so an AI connection may change them
    only on a document that was never published."""
    async with running(database_url, tmp_path) as c:
        slug = await new_doc(c, "AI details probe", TextBlock(html="<p>x</p>"))
        async with module.session() as s:
            doc = await service.by_slug(s, slug)
            assert doc is not None
            await service.update_details(
                s,
                doc,
                title="AI details probe (draft)",
                collection_id=doc.collection_id,
                tags=[],
                who=AI,
            )
            await s.commit()
        assert (await c.post(f"/api/docs/documents/{doc.slug}/publish")).status_code == 303
        async with module.session() as s:
            doc = await service.by_slug(s, doc.slug)
            assert doc is not None and doc.status == "published"
            with pytest.raises(DocsError, match="published document"):
                await service.update_details(
                    s, doc, title="Renamed by AI", collection_id=doc.collection_id, tags=[], who=AI
                )
            await service.update_details(
                s,
                doc,
                title="Renamed by a person",
                collection_id=doc.collection_id,
                tags=[],
                who=HUMAN,
            )
