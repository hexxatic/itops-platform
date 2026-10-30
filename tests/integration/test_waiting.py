"""Waiting for you: what each module has for the person looking, on the home page and as a
count on its nav item; the Admin page; the nav's set order."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import itops_assets
import itops_docs
import itops_news
from itops_assets import service as assets
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, make_app

ADMIN = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
READER = Principal(user="rita@example.com", display_name="Rita",
                   permissions=frozenset({"assets.read", "docs.read", "news.read"}))  # fmt: skip
AI = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}),
               via_ai=True, client="mcp")  # fmt: skip
MODULES = [itops_news.module, itops_assets.module, itops_docs.module]  # loaded in any order


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, MODULES, files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with itops_assets.module.session() as s:
            await s.execute(text("DELETE FROM assets.proposals"))
            await s.execute(text("DELETE FROM assets.records"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


async def test_waiting_for_you_and_the_nav(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        home = (await c.get("/")).text
        assert "proposed asset change" not in home  # (other modules' leftovers may wait)
        # Set order, whatever the load order: Docs, Assets, News, then Admin.
        nav = home[home.index('class="shell-nav"') : home.index("</nav>")]
        assert nav.index("Docs") < nav.index("Assets") < nav.index("News") < nav.index("Admin")
        assert 'href="/assets/"' in home and "Installed modules" not in home  # cards link

        async with itops_assets.module.session() as s:
            with acting_as(AI):
                p = await assets.propose(s, AI, type_key="domain", name="example.org",
                                         fields={}, note="from WHOIS")  # fmt: skip
            await s.commit()
            proposal = p.id
        # It came in over MCP in real life: a POST, which gathers the list again.
        await c.post("/account/appearance", data={"appearance": "auto"})
        home = (await c.get("/")).text
        assert "1 proposed asset change to approve" in home and 'href="/assets/proposals"' in home
        assert 'title="1 waiting for you">1</span>' in home  # the count on Assets' nav item

        # Approving is a change: the list is gathered again at once, not a minute later.
        await c.post(f"/api/assets/proposals/{proposal}/approve")
        assert "proposed asset change" not in (await c.get("/")).text

        admin = (await c.get("/admin")).text
        assert 'href="/connectors"' in admin and 'href="/assets/types"' in admin

        with acting_as(READER):  # nothing to approve, and no admin
            page = (await c.get("/")).text
            assert "Nothing is waiting for you" in page and 'href="/admin"' not in page
            assert (await c.get("/admin")).status_code == 404

        # Every AI tool says where a person reviews what it drafted, the same way.
        reply = itops_assets.module.review("/proposals", "the proposal")
        assert reply["review_url"].endswith("/assets/proposals")
        assert "Waiting for you" in reply["next"] and "You can't approve it" in reply["next"]
