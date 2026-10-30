"""How-tos on their own: conformance, migrations, and what kb does without Docs (nothing to
show, nothing to say about). With Docs and the help site: tests/integration/test_help_site.py."""

from __future__ import annotations

from pathlib import Path

import httpx
from itops_kb import module, service

from itops.sdk import Principal
from itops.testing import assert_conformant, assert_migrations_roundtrip
from itops.testing.app import acting_as, make_app

READER = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"kb.read"}))


def test_conformance() -> None:
    assert_conformant(module)


async def test_migrations_roundtrip(database_url: str) -> None:
    await assert_migrations_roundtrip(module, database_url)


async def test_without_docs_there_are_no_how_tos(database_url: str, tmp_path: Path) -> None:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            with acting_as(READER):
                assert "No how-tos yet" in (await c.get("/kb/")).text
                assert (await c.get("/kb/a/anything")).status_code == 404
                r = await c.post("/api/kb/feedback/anything", data={"helpful": "yes"})
                assert r.status_code == 404
        assert await service.sync() == "Search is up to date."
