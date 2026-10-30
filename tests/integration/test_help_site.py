"""The help site (ADR 0027), end to end: Docs how-tos shown by kb on the help site's host to
staff who hold nothing but the everyone permissions, with their files, search and
feedback; and what the help host and IT-Ops' host each refuse."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import httpx
import itops_docs
import itops_kb
from itops_docs import service as docs
from itops_kb import service as kb
from sqlalchemy import text

from itops.core.app import create_app
from itops.core.config import Settings, SiteConfig
from itops.sdk import Principal
from itops.sdk.content import Content, TextBlock
from itops.testing.app import acting_as, wait_for_search

HELP = "http://help.localhost"
# What the staff role holds once Docs and kb are loaded (the everyone permissions).
STAFF = Principal(
    user="sam@example.com", display_name="Sam",
    permissions=frozenset({"kb.read", "docs.howto"}),
)  # fmt: skip
READER = Principal(
    user="ira@example.com", display_name="Ira",
    permissions=frozenset({"docs.read", "kb.read", "docs.howto"}),
)  # fmt: skip
ADMIN = Principal(user="admin@example.com", display_name="Admin", permissions=frozenset({"*"}))


async def test_staff_read_how_tos_on_the_help_site(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    settings = Settings(
        database_url_override=database_url, files_dir=str(tmp_path), run_jobs=False,
        reindex_on_start=False, sites={"help": SiteConfig(url=HELP, title="IT Help")},
    )  # fmt: skip
    app = create_app(settings, modules=[itops_docs.module, itops_kb.module])
    async with app.router.lifespan_context(app):
        async with itops_kb.module.session() as s:
            await s.execute(text("DELETE FROM kb.feedback"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://localhost") as itops,
            httpx.AsyncClient(transport=transport, base_url=HELP) as helpsite,
        ):
            # IT writes, publishes and shows a how-to on the help site (dev: admin).
            await itops.post("/api/docs/collections", data={"name": f"Help {tag}"})
            async with itops_docs.module.session() as s:
                col = next(c for c, _ in await docs.collections(s) if c.name == f"Help {tag}")
            body = Content(blocks=[TextBlock(html=f"<p>Pick the staff network {tag}.</p>")])
            r = await itops.post(
                "/api/docs/documents",
                data={"title": f"Join the Wi-Fi {tag}", "collection_id": str(col.id),
                      "content": body.model_dump_json()},
                files=[("attachments", ("Steps.pdf", b"%PDF-1.7", "application/pdf"))],
            )  # fmt: skip
            m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
            assert m
            slug = m[1]
            await itops.post(f"/api/docs/documents/{slug}/publish?version=1")
            await itops.post(f"/api/docs/documents/{slug}/help-site", data={"shown": "1"})
            await app.state.runtime.bus.drain()  # kb indexes it on Docs' event
            await wait_for_search(itops_kb.module)

            with acting_as(STAFF):
                # The help site: its own title and menu, its first page at /.
                r = await helpsite.get("/", follow_redirects=False)
                assert r.headers["location"] == "/kb/"
                page = (await helpsite.get("/kb/")).text
                assert "IT Help" in page and f"Join the Wi-Fi {tag}" in page
                assert 'href="/docs/' not in page and 'href="/admin"' not in page
                page = (await helpsite.get(f"/kb/a/{slug}")).text
                assert f"Pick the staff network {tag}." in page and "Steps.pdf" in page
                link = re.search(r'href="(/files/[0-9a-f-]+)"', page)
                assert link
                assert (await helpsite.get(link[1])).content == b"%PDF-1.7"  # Docs' file
                # Search on the help site finds it under How-tos.
                found = (await helpsite.get("/search", params={"q": f"staff network {tag}"})).text
                assert f"/kb/a/{slug}" in found and f"/docs/d/{slug}" not in found
                # Feedback, their own.
                r = await helpsite.post(f"/api/kb/feedback/{slug}", data={"helpful": "yes"})
                assert r.status_code == 303
                # Nothing of IT-Ops answers on the help host...
                for path in ("/docs/", f"/docs/d/{slug}", "/admin", "/audit", "/api/docs/x"):
                    assert (await helpsite.get(path)).status_code == 404, path
                # ...and on IT-Ops' host staff hold nothing but the everyone permissions.
                assert (await itops.get(f"/docs/d/{slug}")).status_code == 403
                r = await itops.get("/", follow_redirects=False)
                assert r.headers["location"] == f"{HELP}/"

            # An IT reader: IT-Ops' search keeps to Docs; its menu has no How-tos.
            with acting_as(READER):
                found = (await itops.get("/search", params={"q": f"staff network {tag}"})).text
                assert f"/docs/d/{slug}" in found and f"/kb/a/{slug}" not in found
                assert 'href="/kb/"' not in (await itops.get("/docs/")).text

            # A how-to for one audience: its people find it; search hides it from others.
            r = await itops.post("/api/docs/audiences", data={"name": f"Site {tag}"})
            audience = r.headers["location"].rsplit("/", 1)[1]
            with acting_as(ADMIN):
                await app.state.runtime.areas.set(
                    "docs", "audience", audience, name=f"Site {tag}", groups=["Site-Example"],
                    no_ai=False,
                )  # fmt: skip
            body = Content(blocks=[TextBlock(html=f"<p>Use the press printer {tag}.</p>")])
            r = await itops.post(
                "/api/docs/documents",
                data={"title": f"Press printer {tag}", "collection_id": str(col.id),
                      "content": body.model_dump_json()},
            )  # fmt: skip
            m = re.match(r"/docs/d/([^?]+)", r.headers["location"])
            assert m
            local = m[1]
            await itops.post(f"/api/docs/documents/{local}/publish?version=1")
            await itops.post(
                f"/api/docs/documents/{local}/help-site",
                data={"shown": "1", "audience_id": audience},
            )
            await app.state.runtime.bus.drain()
            await wait_for_search(itops_kb.module)
            in_group = Principal(
                user="cam@example.com", display_name="Cam", permissions=STAFF.permissions,
                groups=frozenset({"site-example"}),
            )  # fmt: skip
            with acting_as(in_group):
                assert f"Press printer {tag}" in (await helpsite.get("/kb/")).text
                found = (await helpsite.get("/search", params={"q": f"press printer {tag}"})).text
                assert f"/kb/a/{local}" in found
            with acting_as(STAFF):
                assert f"Press printer {tag}" not in (await helpsite.get("/kb/")).text
                assert (await helpsite.get(f"/kb/a/{local}")).status_code == 404
                found = (await helpsite.get("/search", params={"q": f"press printer {tag}"})).text
                assert f"/kb/a/{local}" not in found and f"Press printer {tag}" not in found
                found = (await helpsite.get("/kb/", params={"q": f"press printer {tag}"})).text
                assert f"/kb/a/{local}" not in found

            # Archived in Docs: gone from the help site and its search.
            await itops.post(f"/api/docs/documents/{slug}/archive")
            await app.state.runtime.bus.drain()
            with acting_as(STAFF):
                assert (await helpsite.get(f"/kb/a/{slug}")).status_code == 404
                assert (await helpsite.get(link[1])).status_code == 403
            assert slug not in {a["slug"] for a in await kb.articles()}
