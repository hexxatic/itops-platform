"""Entra sign-in mode: fail closed, sign-in callback (MSAL mocked), roles from groups,
personal access tokens on the REST API and MCP."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from itops_docs import module as docs
from pydantic import SecretStr

from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.entra import EntraAuth, _safe_next  # pyright: ignore[reportPrivateUsage]
from itops.core.identity import permissions_for, roles_from_groups

BASE = "http://localhost:8800"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class FakeMsal:
    """Stands in for msal.ConfidentialClientApplication: Entra says who signed in."""

    def __init__(self, groups: list[str]) -> None:
        self.oid = str(uuid.uuid4())
        self.groups = groups

    def initiate_auth_code_flow(self, scopes: list[str], redirect_uri: str) -> dict[str, Any]:
        assert redirect_uri == f"{BASE}/auth/callback"
        return {"auth_uri": "https://login.example/authorize?state=s1", "state": "s1"}

    def acquire_token_by_auth_code_flow(
        self, flow: dict[str, Any], query: dict[str, str]
    ) -> dict[str, Any]:
        if query.get("state") != flow["state"]:
            return {"error": "state_mismatch", "error_description": "State mismatch"}
        return {
            "id_token_claims": {
                "oid": self.oid,
                "preferred_username": f"{self.oid[:8]}@example.com",
                "name": "Test Person",
                "groups": self.groups,
            }
        }


@asynccontextmanager
async def running(database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url_override=database_url,
        auth_mode="entra",
        entra_tenant_id="tenant",
        entra_client_id="client",
        entra_client_secret=SecretStr("secret"),
        directory_check=False,  # test users aren't in AD (test_oauth covers the check)
    )
    app = create_app(settings, modules=[docs])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            yield c


def entra_says(monkeypatch: pytest.MonkeyPatch, groups: list[str]) -> None:
    fake = FakeMsal(groups)
    monkeypatch.setattr(EntraAuth, "_app", lambda self: fake)  # pyright: ignore[reportUnknownLambdaType]


async def sign_in(client: httpx.AsyncClient, next_path: str = "/") -> httpx.Response:
    r = await client.get(f"/auth/login?next={next_path}")
    # A page, not a Location header (IIS ARR would rewrite the header's host).
    assert r.status_code == 200 and "location" not in r.headers
    assert "url=https://login.example/authorize?state=s1" in r.text
    return await client.get("/auth/callback?code=abc&state=s1")


# ---- pure ----------------------------------------------------------------------------


def test_roles_from_groups_ignores_domain_and_case() -> None:
    groups = {"reader": "App-ITOps-Readers", "editor": "App-ITOps-Editors"}
    assert roles_from_groups(["CORP\\app-itops-editors", "Other"], groups) == ["editor"]
    assert roles_from_groups(["Domain Users"], groups) == []


def test_roles_grant_permissions_by_last_word() -> None:
    declared = ["docs.read", "docs.write", "docs.publish", "helpdesk.read"]
    assert permissions_for(["reader"], declared) == {"docs.read", "helpdesk.read"}
    editor = {"docs.read", "docs.write", "helpdesk.read"}
    assert permissions_for(["reader", "editor"], declared) == editor
    assert "docs.publish" in permissions_for(["publisher"], declared)
    assert permissions_for(["admin"], declared) == {"*"}
    assert permissions_for([], declared) == frozenset()


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "", None])
def test_sign_in_never_redirects_off_site(target: str | None) -> None:
    assert _safe_next(target) == "/"


# ---- fail closed -----------------------------------------------------------------------


async def test_anonymous_requests_are_refused(database_url: str) -> None:
    async with running(database_url) as client:
        r = await client.get("/docs/")
        assert r.status_code == 303
        assert r.headers["location"] == "/auth/login?next=/docs/"
        assert (await client.post("/api/docs/documents", json={})).status_code == 401
        assert (await client.post("/mcp/", headers=MCP_HEADERS, json={})).status_code == 401
        assert (await client.get("/health")).status_code == 200


async def test_other_host_names_redirect_to_the_public_address(database_url: str) -> None:
    async with running(database_url) as client:
        r = await client.get("http://127.0.0.1:8800/docs/?q=1")
        assert r.status_code == 307
        assert r.headers["location"] == f"{BASE}/docs/?q=1"
        # Health is checked on loopback by deploys and monitoring: never redirected.
        assert (await client.get("http://127.0.0.1:8810/health")).status_code == 200


# ---- browser sign-in -------------------------------------------------------------------


async def test_sign_in_with_a_role_group(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra_says(monkeypatch, ["App-ITOps-Editors"])
    async with running(database_url) as client:
        r = await sign_in(client, "/docs/")
        assert r.status_code == 303 and r.headers["location"] == "/docs/"
        page = await client.get("/docs/")
        assert page.status_code == 200 and "Test Person" in page.text
        account = await client.get("/account")
        assert "editor" in account.text
        assert (await client.get("/audit")).status_code == 403  # admins only
        await client.get("/auth/logout")
        assert (await client.get("/docs/")).status_code == 303


async def test_appearance_follows_the_person_to_the_next_sign_in(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra_says(monkeypatch, ["App-ITOps-Readers"])
    async with running(database_url) as client:
        await sign_in(client)
        await client.post("/account/appearance", data={"appearance": "light"})
        await client.get("/auth/logout")
        await sign_in(client)  # a fresh session reads it from the user row
        assert 'data-theme="light"' in (await client.get("/docs/")).text
        await client.post("/account/appearance", data={"appearance": "auto"})


async def test_sign_in_without_a_role_group_is_refused(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra_says(monkeypatch, ["Domain Users"])
    async with running(database_url) as client:
        r = await sign_in(client)
        assert r.status_code == 403
        assert "not set up for IT-Ops" in r.text
        assert (await client.get("/docs/")).status_code == 303


async def test_callback_rejects_a_forged_state(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra_says(monkeypatch, ["App-ITOps-Admins"])
    async with running(database_url) as client:
        await client.get("/auth/login")
        r = await client.get("/auth/callback?code=abc&state=forged")
        assert r.status_code == 403
        assert (await client.get("/docs/")).status_code == 303


# ---- personal access tokens ------------------------------------------------------------


async def test_token_lifecycle_rest_and_mcp(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra_says(monkeypatch, ["App-ITOps-Publishers"])
    async with running(database_url) as client:
        await sign_in(client)
        r = await client.post("/account/tokens", data={"name": "Claude", "days": "30"})
        match = re.search(r"itops_[A-Za-z0-9_-]{40,}", r.text)
        assert match, "the new token is shown once"
        token = match.group(0)
        assert token not in (await client.get("/account")).text, "and never again"

        async with httpx.AsyncClient(transport=client._transport, base_url=BASE) as tool:  # pyright: ignore[reportPrivateUsage]
            bearer = {"Authorization": f"Bearer {token}"}
            # A token acts via AI: even a publisher can't publish with it.
            r = await tool.post("/api/docs/documents/anything/publish", headers=bearer)
            assert r.status_code == 403
            assert "human-only" in r.text

            names = await mcp_tool_names(tool, bearer)
            assert "docs_search" in names
            result = await mcp_call(tool, bearer, "docs_list_collections")
            assert '"isError":false' in result.replace(" ", "")

            # Revoked: refused everywhere.
            account = (await client.get("/account")).text
            token_id = re.search(r"/account/tokens/(\d+)/revoke", account)
            assert token_id
            await client.post(f"/account/tokens/{token_id.group(1)}/revoke")
            r = await tool.post("/api/docs/documents", headers=bearer, json={})
            assert r.status_code == 401
            r = await tool.post("/mcp/", headers={**MCP_HEADERS, **bearer}, json=_initialize())
            assert r.status_code == 401


def _initialize() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }


async def _session(client: httpx.AsyncClient, bearer: dict[str, str]) -> dict[str, str]:
    r = await client.post("/mcp/", headers={**MCP_HEADERS, **bearer}, json=_initialize())
    assert r.status_code == 200, r.text
    headers = {**MCP_HEADERS, **bearer, "mcp-session-id": r.headers["mcp-session-id"]}
    await client.post(
        "/mcp/", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    return headers


async def mcp_tool_names(client: httpx.AsyncClient, bearer: dict[str, str]) -> str:
    headers = await _session(client, bearer)
    r = await client.post(
        "/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    return r.text


async def mcp_call(client: httpx.AsyncClient, bearer: dict[str, str], name: str) -> str:
    headers = await _session(client, bearer)
    r = await client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
    )
    return r.text


async def test_sign_in_and_tokens_are_audited(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra_says(monkeypatch, ["App-ITOps-Admins"])
    async with running(database_url) as client:
        await sign_in(client)
        await client.post("/account/tokens", data={"name": "Audit probe", "days": "7"})
        token_id = re.search(r"/account/tokens/(\d+)/revoke", (await client.get("/account")).text)
        assert token_id
        await client.post(f"/account/tokens/{token_id.group(1)}/revoke")
        trail = (await client.get("/audit?module=itops&who=Test+Person")).text
        assert "Signed in as admin" in trail
        assert "Created access token “Audit probe” (7 days)" in trail
        assert "Revoked access token “Audit probe”" in trail


async def test_reverse_proxy_host_is_trusted_from_loopback_only(database_url: str) -> None:
    """IIS on the same machine proxies with Host=127.0.0.1:port and the real name in
    X-Forwarded-Host. Trusted from loopback; ignored from anywhere else."""
    proxied = {"x-forwarded-host": "localhost:8800"}
    async with running(database_url) as client:
        r = await client.get("http://127.0.0.1:8810/docs/", headers=proxied)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url_override=database_url,
        auth_mode="entra",
        entra_tenant_id="tenant",
        entra_client_id="client",
        entra_client_secret=SecretStr("secret"),
    )
    app = create_app(settings, modules=[docs])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("10.1.2.3", 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8810") as c:
            r = await c.get("/docs/", headers=proxied)
            assert r.status_code == 307  # header ignored: redirected to the public name
            assert r.headers["location"] == f"{BASE}/docs/"
