"""Assets pages: lists with search, record pages, create/edit/delete, the proposals queue."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from itops_assets import module, service
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, make_app

AI = Principal(
    user="pat@example.com",
    display_name="Pat",
    permissions=frozenset({"*"}),
    via_ai=True,
    client="mcp",
)
PERSON = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
READER = Principal(user="rita", display_name="Rita", permissions=frozenset({"assets.read"}))
EDITOR = Principal(
    user="ed", display_name="Ed", permissions=frozenset({"assets.read", "assets.write"})
)


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM assets.proposals"))
            await s.execute(text("DELETE FROM assets.records"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c


def record_id(location: str) -> str:
    m = re.fullmatch(r"/assets/r/([0-9a-f-]+)", location)
    assert m, location
    return m[1]


async def test_create_list_search_edit_delete(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        r = await c.post(
            "/api/assets/records",
            data={"type_key": "location", "name": "North plant", "f__city": "Northville"},
        )
        plant = record_id(r.headers["location"])

        # The new-device form offers the location in its picker.
        form = (await c.get("/assets/t/device/new")).text
        assert f'value="assets:location:{plant}"' in form and "North plant" in form

        r = await c.post(
            "/api/assets/records",
            data={
                "type_key": "device",
                "name": "DB-SRV01",
                "f__kind": "Server",
                "f__primary_ip": "192.0.2.15",
                "f__location": f"assets:location:{plant}",
            },
        )
        sql = record_id(r.headers["location"])

        page = (await c.get(f"/assets/r/{sql}")).text
        assert "192.0.2.15" in page
        assert f'href="/assets/r/{plant}"' in page  # the link shows as the location's name

        listing = (await c.get("/assets/t/device")).text
        assert "DB-SRV01" in listing and "192.0.2.15" in listing
        await c.post(
            "/api/assets/records",
            data={
                "type_key": "ip_address",
                "name": "192.0.2.15",
                "f__switch": f"assets:device:{sql}",
            },
        )
        assert "DB-SRV01" in (await c.get("/assets/t/ip_address")).text  # linked name in a column
        assert "DB-SRV01" in (await c.get("/assets/t/device?q=2.15")).text
        assert "DB-SRV01" not in (await c.get("/assets/t/device?q=nothing-like-this")).text

        # Problems come back as a message on the form.
        r = await c.post(
            "/api/assets/records", data={"type_key": "ssl_certificate", "name": "itops.example.com"}
        )
        assert r.headers["location"].startswith("/assets/t/ssl_certificate/new?error=Valid")

        r = await c.post(
            f"/api/assets/records/{sql}",
            data={"name": "DB-SRV01", "f__kind": "Server", "f__primary_ip": "192.0.2.16"},
        )
        assert r.headers["location"] == f"/assets/r/{sql}"
        page = (await c.get(f"/assets/r/{sql}")).text
        assert "192.0.2.16" in page and "Changed location, primary_ip" in page
        assert "192.0.2.16" in (await c.get(f"/assets/r/{sql}/edit")).text

        r = await c.post(f"/api/assets/records/{sql}/delete")
        assert r.headers["location"] == "/assets/t/device"
        assert (await c.get(f"/assets/r/{sql}")).status_code == 404


async def test_a_failed_save_keeps_what_was_typed(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        r = await c.post("/api/assets/records", data={"type_key": "device", "name": "APP-SRV01"})
        app_p = record_id(r.headers["location"])
        # A certificate without its required expiry date: everything typed comes back.
        r = await c.post("/api/assets/records", data={
            "type_key": "ssl_certificate", "name": "vpn.example.com",
            "f__issued_by": "Sectigo", "f__installed_on": f"assets:device:{app_p}"})  # fmt: skip
        location = r.headers["location"]
        assert "error=Valid%20until%20is%20required" in location and "kept=" in location
        form = (await c.get(location)).text
        assert 'value="vpn.example.com"' in form and 'value="Sectigo"' in form
        assert f'value="assets:device:{app_p}" selected' in form
        with acting_as(EDITOR):  # only the person who typed it gets it back
            assert "vpn.example.com" not in (await c.get(location)).text

        r = await c.post(f"/api/assets/records/{app_p}", data={"name": "  "})
        assert "kept=" in r.headers["location"]
        assert 'value="APP-SRV01"' not in (await c.get(r.headers["location"])).text


async def test_who_can_do_what(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        r = await c.post("/api/assets/records", data={"type_key": "domain", "name": "example.com"})
        rid = record_id(r.headers["location"])
        with acting_as(READER):
            page = (await c.get(f"/assets/r/{rid}")).text
            assert "example.com" in page and ">Edit<" not in page and ">Delete<" not in page
            assert (await c.get("/assets/t/domain/new")).status_code == 403
            r = await c.post(f"/api/assets/records/{rid}", data={"name": "x.com"})
            assert r.status_code == 403
        with acting_as(EDITOR):
            page = (await c.get(f"/assets/r/{rid}")).text
            assert ">Edit<" in page and ">Delete<" not in page
            assert (await c.post(f"/api/assets/records/{rid}/delete")).status_code == 403
            assert (await c.get("/assets/proposals")).status_code == 403
        with acting_as(AI):  # human-only, even with every permission
            assert (await c.post(f"/api/assets/records/{rid}/delete")).status_code == 403


async def test_proposals_queue(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as c:
        async with module.session() as s:
            new = await service.propose(
                s,
                AI,
                type_key="domain",
                name="example.org",
                fields={"registrar": "Gandi"},
                note="from WHOIS",
            )
            existing = await service.create(
                s, PERSON, type_key="domain", name="example.net", fields={"registrar": "Gandi"}
            )
            change = await service.propose(
                s, AI, record=existing, name="example.net", fields={"registrar": "Cloudflare"}
            )
            await s.commit()
            new_id, change_id, existing_id = new.id, change.id, existing.id

        assert "2 proposed changes" in (await c.get("/assets/")).text
        queue = (await c.get("/assets/proposals")).text
        assert "New domain: example.org" in queue and "from WHOIS" in queue
        assert "Change to example.net" in queue and "Cloudflare" in queue

        r = await c.post(f"/api/assets/proposals/{new_id}/approve")
        assert r.headers["location"] == "/assets/proposals"
        await c.post(f"/api/assets/proposals/{change_id}/reject")
        assert "Nothing waiting" in (await c.get("/assets/proposals")).text

        async with module.session() as s:
            assert [r.name for r in await service.records(s, type_key="domain")] == [
                "example.net",
                "example.org",
            ]
            kept = await service.get(s, existing_id)
            assert kept and service.record_fields(kept) == {"registrar": "Gandi"}

        r = await c.post(f"/api/assets/proposals/{new_id}/approve")
        assert "already%20decided" in r.headers["location"]
        with acting_as(AI):
            assert (await c.post(f"/api/assets/proposals/{change_id}/approve")).status_code == 403


async def test_a_field_called_name_never_hides_the_record_link(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as c:
        runtime = module.runtime
        async with module.session() as s:
            if await runtime.types.get(s, "assets", "gadget") is None:
                await runtime.types.draft(s, "assets", {"name": "Gadget", "fields": [
                    {"key": "name", "label": "Name", "kind": "text"},
                    {"key": "colour", "label": "Colour", "kind": "text"}]})  # fmt: skip
                await runtime.types.publish(s, "assets", "gadget")
            await s.commit()
        form = {"type_key": "gadget", "name": "Widget 9", "f__name": "Widget", "f__colour": "Blue"}
        r = await c.post("/api/assets/records", data=form)
        listing = (await c.get("/assets/t/gadget")).text
        assert f'href="/assets/r/{record_id(r.headers["location"])}"' in listing
        assert listing.count(">Name<") == 1 and "Blue" in listing
