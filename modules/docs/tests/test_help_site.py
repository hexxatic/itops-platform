"""Documents shown on the help site (ADR 0027, 0028): a publisher shows one, for everyone or
an audience; the how-to services offer published ones and nothing else (drafts, documents
not shown, restricted collections), open their files, and leave an audience's out for people
in none of its groups, while IT's Docs pages ignore audiences."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from itops_docs import module, service
from sqlalchemy import text

from itops.sdk import Event, Principal
from itops.sdk.content import Content, HeadingBlock, TextBlock
from itops.testing.app import acting_as, make_app

ADMIN = Principal(user="admin@example.com", display_name="Admin", permissions=frozenset({"*"}))
READER = Principal(
    user="reader@example.com", display_name="Reader", permissions=frozenset({"docs.read"})
)
EDITOR = Principal(
    user="ed@example.com", display_name="Ed", permissions=frozenset({"docs.read", "docs.write"})
)
STAFF = Principal(user="sam@example.com", display_name="Sam", permissions=frozenset({"docs.howto"}))
STAFF_IN_GROUP = Principal(
    user="cam@example.com", display_name="Cam", permissions=frozenset({"docs.howto"}),
    groups=frozenset({"site-example"}),
)  # fmt: skip


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, Any]]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with runtime.sessionmaker() as s:
            await s.execute(text("DELETE FROM itops.area_restrictions"))
            await s.commit()
        await runtime.areas.reload()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            try:
                yield c, app
            finally:
                async with runtime.sessionmaker() as s:
                    await s.execute(text("DELETE FROM itops.area_restrictions"))
                    await s.commit()


async def _collection(c: httpx.AsyncClient, name: str) -> tuple[int, str]:
    r = await c.post("/api/docs/collections", data={"name": name})
    assert r.headers["location"] == "/docs/collections", r.headers["location"]
    async with module.session() as s:
        col = next(col for col, _ in await service.collections(s) if col.name == name)
        return col.id, col.slug


async def _document(
    c: httpx.AsyncClient,
    collection_id: int,
    title: str,
    body: str,
    *,
    publish: bool = True,
    shown: bool = True,
    audience: int | None = None,
) -> str:
    content = Content(
        blocks=[HeadingBlock(text="Before you start", level=2), TextBlock(html=f"<p>{body}</p>")]
    ).model_dump_json()
    r = await c.post(
        "/api/docs/documents",
        data={"title": title, "collection_id": str(collection_id), "content": content,
              "tags": "wifi"},
    )  # fmt: skip
    m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
    assert m, r.headers["location"]
    if publish:
        await c.post(f"/api/docs/documents/{m[1]}/publish?version=1")
    if shown:
        await _show(c, m[1], audience=audience)
    return m[1]


async def _show(
    c: httpx.AsyncClient, slug: str, *, shown: bool = True, audience: int | None = None
) -> httpx.Response:
    data = {"audience_id": str(audience) if audience else ""}
    if shown:
        data["shown"] = "1"
    return await c.post(f"/api/docs/documents/{slug}/help-site", data=data)


async def _doc_id(slug: str) -> str:
    async with module.session() as s:
        doc = await service.by_slug(s, slug)
        assert doc is not None
        return str(doc.id)


async def _audience(c: httpx.AsyncClient, name: str) -> int:
    r = await c.post("/api/docs/audiences", data={"name": name})
    m = re.match(r"/docs/audiences/(\d+)", r.headers["location"])
    assert m, r.headers["location"]
    return int(m[1])


def _keep(seen: list[dict[str, Any]]) -> Any:
    async def handler(event: Event) -> None:
        seen.append(event.payload)

    return handler


async def test_only_published_documents_shown_on_the_help_site(
    database_url: str, tmp_path: Path
) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, _):
        col_id, col_slug = await _collection(c, f"Wi-Fi {tag}")
        empty_id, empty_slug = await _collection(c, f"Runbooks {tag}")
        shown = await _document(
            c, col_id, f"Join the Wi-Fi {tag}", f"Pick the staff network {tag}."
        )
        draft = await _document(c, col_id, f"Draft how-to {tag}", "Not yet.", publish=False)
        internal = await _document(
            c, col_id, f"Rebuild the controller {tag}", "IT only.", shown=False
        )
        await _document(c, empty_id, f"Patch the switch {tag}", "IT only.", shown=False)

        listed = await service.howtos(col_slug)
        assert [d["slug"] for d in listed] == [shown]
        offered = [col["slug"] for col in await service.howto_collections()]
        assert col_slug in offered and empty_slug not in offered  # nothing shown in it
        summary = listed[0]
        assert summary["collection"] == {"slug": col_slug, "name": f"Wi-Fi {tag}"}
        assert summary["audience"] is None and summary["area"] is None
        assert summary["excerpt"] == f"Pick the staff network {tag}."
        assert summary["tags"] == ["wifi"] and summary["version"] == 1

        slugs = {d["slug"] for d in await service.howtos()}
        assert shown in slugs and draft not in slugs and internal not in slugs

        doc = await service.howto(shown)
        assert doc is not None and doc["title"] == f"Join the Wi-Fi {tag}"
        assert doc["content"].blocks[1].type == "text" and doc["attachments"] == []
        for missing in (draft, internal, f"no-such-{tag}"):
            assert await service.howto(missing) is None

        # Its files open to everyone; a document not shown, or a draft, keeps them.
        assert await service.shared_file_permission("document", await _doc_id(shown)) == (
            "docs.howto"
        )
        assert await service.shared_file_permission("document", await _doc_id(internal)) is None
        assert await service.shared_file_permission("document", await _doc_id(draft)) is None

        # The draft shown on the help site appears once it's published.
        await c.post(f"/api/docs/documents/{draft}/publish?version=1")
        assert await service.howto(draft) is not None

        # An edit stays a draft: staff keep seeing the published version.
        content = Content(blocks=[TextBlock(html="<p>Changed.</p>")]).model_dump_json()
        await c.post(
            f"/api/docs/documents/{shown}/save", data={"content": content, "change_note": "x"}
        )
        doc = await service.howto(shown)
        assert doc is not None and doc["version"] == 1
        assert doc["excerpt"] == f"Pick the staff network {tag}."
        assert "On the help site: everyone" in (await c.get(f"/docs/d/{shown}")).text


async def test_taking_a_document_off_hides_it(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, _):
        col_id, _ = await _collection(c, f"Guides {tag}")
        slug = await _document(c, col_id, f"Printer setup {tag}", "Add the printer.")
        assert await service.howto(slug) is not None

        seen: list[dict[str, Any]] = []
        module.runtime.bus.subscribe("docs.help_site_changed", _keep(seen), subscriber="test")
        r = await _show(c, slug, shown=False)
        assert r.status_code == 303
        await module.runtime.bus.drain()
        assert await service.howto(slug) is None
        assert any(e.get("slug") == slug for e in seen)
        rows = await _audit(await _doc_id(slug), "document.help_site")
        assert rows[-1] == "Taken off the help site"
        assert "Shown on the help site for everyone" in rows


async def _audit(entity_id: str, action: str) -> list[str]:
    async with module.session() as s:
        rows = await s.execute(
            text(
                "SELECT summary FROM itops.audit_log WHERE module = 'docs' AND action = :a"
                " AND entity_id = :i ORDER BY id"
            ),
            {"a": action, "i": entity_id},
        )
        return [r[0] for r in rows.all()]


async def test_a_restricted_collection_isnt_offered(database_url: str, tmp_path: Path) -> None:
    """Restricted to groups (ADR 0012), it's not for everyone, whoever is asking."""
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        col_id, col_slug = await _collection(c, f"Private how-tos {tag}")
        slug = await _document(c, col_id, f"Secret how-to {tag}", "Shh.")
        doc_id = await _doc_id(slug)
        with acting_as(ADMIN):
            await app.state.runtime.areas.set(
                "docs", "collection", str(col_id), name=col_slug, groups=["App-Example"],
                no_ai=False,
            )  # fmt: skip
        for who in (READER, ADMIN):
            with acting_as(who):
                assert await service.howto(slug) is None
                assert slug not in {d["slug"] for d in await service.howtos()}
                assert col_slug not in {col["slug"] for col in await service.howto_collections()}
                assert await service.shared_file_permission("document", doc_id) is None
        assert slug not in {d["slug"] for d in await service.howto_index()}


async def test_only_publishers_show_a_document(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, _):
        col_id, _ = await _collection(c, f"Locked {tag}")
        slug = await _document(c, col_id, f"Not yet {tag}", "Hm.", shown=False)
        with acting_as(EDITOR):
            assert (await _show(c, slug)).status_code == 403
            assert (
                'action="/api/docs/documents/' + slug + '/help-site"'
                not in (await c.get(f"/docs/d/{slug}")).text
            )
        assert await service.howto(slug) is None


async def test_an_audience_narrows_the_help_site(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    async with running(database_url, tmp_path) as (c, app):
        col_id, col_slug = await _collection(c, f"Plant {tag}")
        audience = await _audience(c, f"Site {tag}")
        everyone = await _document(c, col_id, f"Badge in {tag}", "Tap the reader.")
        local = await _document(
            c, col_id, f"Use the press printer {tag}", "Pick PRESS01.", audience=audience
        )
        local_id = await _doc_id(local)
        page = (await c.get(f"/docs/d/{local}")).text
        assert f"On the help site: Site {tag}" in page
        assert "no groups yet: everyone" in page  # the picker says it reaches everyone

        # No groups yet: it reaches everyone.
        with acting_as(STAFF):
            assert await service.howto(local) is not None

        with acting_as(ADMIN):
            await app.state.runtime.areas.set(
                "docs", "audience", str(audience), name=f"Site {tag}", groups=["Site-Example"],
                no_ai=False,
            )  # fmt: skip
        with acting_as(STAFF):
            assert {d["slug"] for d in await service.howtos(col_slug)} == {everyone}
            assert await service.howto(local) is None
            assert not await service.visible_entity("document", local_id)
        with acting_as(STAFF_IN_GROUP):
            assert {d["slug"] for d in await service.howtos(col_slug)} == {everyone, local}
            found = await service.howto(local)
            assert found is not None and found["audience"] == f"Site {tag}"
            assert await service.visible_entity("document", local_id)
        # IT's Docs ignore audiences.
        with acting_as(READER):
            assert (await c.get(f"/docs/d/{local}")).status_code == 200
            assert await service.visible_entity("document", local_id)
        # The search index gets every how-to, each with its audience's area.
        areas = {d["slug"]: d["area"] for d in await service.howto_index()}
        assert areas[everyone] is None and areas[local] == f"docs:audience:{audience}"

        # An audience documents are for can't be deleted (that would widen who sees them).
        r = await c.post(f"/api/docs/audiences/{audience}/delete")
        assert "error=" in r.headers["location"]
        await _show(c, local, audience=None)
        r = await c.post(f"/api/docs/audiences/{audience}/delete")
        assert r.headers["location"] == "/docs/audiences"
        # Audience ids restart when a test recreates the schema; the audit trail doesn't.
        assert f"Deleted audience “Site {tag}”" in await _audit(str(audience), "audience.deleted")


async def test_only_admins_manage_audiences(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (c, _):
        with acting_as(EDITOR):
            assert (await c.post("/api/docs/audiences", data={"name": "x"})).status_code == 403
            assert (await c.get("/docs/audiences")).status_code == 403
