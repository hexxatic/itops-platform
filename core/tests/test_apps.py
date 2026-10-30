"""App tokens (ADR 0021): another internal application reads the REST API as itself."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from _helpers import MakeModule
from pydantic import SecretStr

from itops.core import auth
from itops.core.app import create_app
from itops.core.apps import readable
from itops.core.areas import Restriction
from itops.core.config import Settings
from itops.core.entra import EntraAuth
from itops.sdk import Module

BASE = "http://localhost:8800"

BODY = """
from typing import Annotated
from fastapi import Depends
from itops.sdk import Principal

@module.router.get("/whoami")
async def whoami(p: Annotated[Principal, Depends(module.require("probe.read"))]) -> dict:
    return {"user": p.user, "client": p.client, "via_ai": p.via_ai}

@module.router.post("/things")
async def write(p: Annotated[Principal, Depends(module.require("probe.write"))]) -> dict:
    return {"ok": True}
"""

MANIFEST = """
title = "Probe"
[permissions]
"probe.read" = "Read"
"probe.write" = "Write"
"probe.publish" = { description = "Publish", human_only = true }
"""


class FakeMsal:
    def __init__(self, groups: list[str]) -> None:
        self.oid = str(uuid.uuid4())
        self.groups = groups

    def initiate_auth_code_flow(self, scopes: list[str], redirect_uri: str) -> dict[str, Any]:
        return {"auth_uri": "https://login.example/authorize?state=s1", "state": "s1"}

    def acquire_token_by_auth_code_flow(
        self, flow: dict[str, Any], query: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "id_token_claims": {
                "oid": self.oid,
                "preferred_username": f"{self.oid[:8]}@example.com",
                "name": "Test Admin",
                "groups": self.groups,
            }
        }


@pytest.fixture
def probe(make_module: MakeModule) -> Module:
    return make_module("probe", manifest_extra=MANIFEST, body=BODY)


@asynccontextmanager
async def running(database_url: str, probe: Module) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url_override=database_url,
        auth_mode="entra",
        entra_tenant_id="tenant",
        entra_client_id="client",
        entra_client_secret=SecretStr("secret"),
        directory_check=False,
    )
    app = create_app(settings, modules=[probe])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            yield c


async def sign_in_admin(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMsal(["App-ITOps-Admins"])
    monkeypatch.setattr(EntraAuth, "_app", lambda self: fake)  # pyright: ignore[reportUnknownLambdaType]
    await client.get("/auth/login?next=/")
    await client.get("/auth/callback?code=abc&state=s1")


async def issue(client: httpx.AsyncClient, **form: str) -> httpx.Response:
    data = {"slug": "help-desk", "name": "Help site (test)", "days": "30", **form}
    return await client.post("/apps/tokens", data=data)


# ---- pure ----------------------------------------------------------------------------


def test_only_reads_that_are_not_human_only_can_be_given() -> None:
    auth.set_human_only({"x.read.secret.read"})
    try:
        got = readable(["a.read", "a.write", "a.publish", "x.read.secret.read", "a.read"])
    finally:
        auth.set_human_only(set())
    assert got == ["a.read"]


def test_apps_never_see_restricted_areas() -> None:
    app = auth.Principal("app:help-desk", "Help (app)", frozenset({"docs.read"}), client="app")
    assert Restriction("docs", "collection", "7", ("App-Security",), no_ai=False).hides_from(app)
    assert Restriction("docs", "collection", "7", (), no_ai=True).hides_from(app)
    assert not Restriction("docs", "collection", "7", (), no_ai=False).hides_from(app)


# ---- end to end ----------------------------------------------------------------------


async def test_an_app_token_reads_the_api_as_the_app(
    database_url: str, probe: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(database_url, probe) as client:
        await sign_in_admin(client, monkeypatch)
        page = (await client.get("/apps")).text
        assert "probe.read" in page and "probe.write" not in page and "probe.publish" not in page

        r = await issue(client, **{"perm__probe.read": "1"})
        match = re.search(r"itops_app_[A-Za-z0-9_-]{40,}", r.text)
        assert match, "the new token is shown once"
        token = match.group(0)
        assert token not in (await client.get("/apps")).text, "and never again"

        async with httpx.AsyncClient(transport=client._transport, base_url=BASE) as app:  # pyright: ignore[reportPrivateUsage]
            bearer = {"Authorization": f"Bearer {token}"}
            r = await app.get("/api/probe/whoami", headers=bearer)
            assert r.status_code == 200, r.text
            assert r.json() == {"user": "app:help-desk", "client": "app", "via_ai": False}
            # Reads only, the API only, never MCP.
            assert (await app.post("/api/probe/things", headers=bearer)).status_code == 403
            assert (await app.get("/", headers=bearer)).status_code == 401
            # Files are checked like anyone's: an unknown one is 404, not refused at the door.
            missing = await app.get(f"/files/{uuid.uuid4()}", headers=bearer)
            assert missing.status_code == 404
            mcp = await app.post(
                "/mcp/",
                headers={**bearer, "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            assert mcp.status_code == 401
            # Nor can it issue tokens itself.
            assert (await app.get("/apps", headers=bearer)).status_code == 401

            token_id = re.search(r"/apps/tokens/(\d+)/revoke", (await client.get("/apps")).text)
            assert token_id
            await client.post(f"/apps/tokens/{token_id.group(1)}/revoke")
            assert (await app.get("/api/probe/whoami", headers=bearer)).status_code == 401

        trail = (await client.get("/audit?module=itops")).text
        assert "Issued an app token for “Help site (test)” (30 days): probe.read" in trail
        assert "Revoked an app token for “Help site (test)”" in trail


async def test_issuing_refuses_writes_and_keeps_the_form(
    database_url: str, probe: Module, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(database_url, probe) as client:
        await sign_in_admin(client, monkeypatch)
        r = await issue(client, **{"perm__probe.write": "1"})
        assert "only be given read permissions" in r.text
        assert "itops_app_" not in r.text
        assert 'value="help-desk"' in r.text, "the form keeps what was typed"
        r = await issue(client, slug="Not A Slug", **{"perm__probe.read": "1"})
        assert "short name" in r.text and "itops_app_" not in r.text
        r = await issue(client)
        assert "at least one permission" in r.text
