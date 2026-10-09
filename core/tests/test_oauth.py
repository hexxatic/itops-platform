"""OAuth for cloud AI clients like ChatGPT (ADR 0013), end to end with Entra mocked:
discovery, registration (only known clients), authorize -> sign-in -> consent -> code ->
tokens (PKCE), using the token, refresh rotation, single-use codes, disconnecting, and the
public MCP host answering for the MCP surface only."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from itops_docs import module as docs
from pydantic import SecretStr
from sqlalchemy import text

from itops.core import identity as identity_module
from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.directory import DirectoryAnswer, parse
from itops.core.entra import EntraAuth
from itops.core.oauth import redirect_allowed

BASE = "http://localhost:8800"
MCP = "http://127.0.0.1:8900"  # the public MCP host (a Cloudflare Tunnel name in production)
CHATGPT = "https://chatgpt.com/connector_platform_oauth_redirect"


class FakeMsal:
    def __init__(self, groups: list[str]) -> None:
        self.oid = str(uuid.uuid4())
        self.groups = groups

    def initiate_auth_code_flow(self, scopes: list[str], redirect_uri: str) -> dict[str, Any]:
        return {"auth_uri": "https://login.example/authorize?state=s1", "state": "s1"}

    def acquire_token_by_auth_code_flow(
        self, flow: dict[str, Any], query: dict[str, str]
    ) -> dict[str, Any]:
        claims = {"oid": self.oid, "preferred_username": f"{self.oid[:8]}@x.com",
                  "name": "Pat Example", "groups": self.groups}  # fmt: skip
        return {"id_token_claims": claims}


@asynccontextmanager
async def running(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    directory: Any = None,
    groups: list[str] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    fake = FakeMsal(groups or ["App-ITOps-Editors"])
    monkeypatch.setattr(EntraAuth, "_app", lambda self: fake)  # pyright: ignore[reportUnknownLambdaType]
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database_url_override=database_url, auth_mode="entra", entra_tenant_id="tenant",
        entra_client_id="client", entra_client_secret=SecretStr("secret"), mcp_public_url=MCP,
        directory_check=False,  # test users aren't in AD; a fake stands in where needed
    )  # fmt: skip
    app = create_app(settings, modules=[docs])
    if directory is not None:
        app.state.identity.directory = directory
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            yield c


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_only_known_clients_may_redirect() -> None:
    hosts = ["chatgpt.com", "claude.ai", "localhost"]
    assert redirect_allowed(CHATGPT, hosts)
    assert redirect_allowed("http://localhost:33418/callback", hosts)
    assert not redirect_allowed("http://chatgpt.com/cb", hosts)  # https only, except localhost
    assert not redirect_allowed("https://chatgpt.com.evil.example/cb", hosts)
    assert not redirect_allowed("https://evil.example/cb", hosts)


async def test_chatgpt_connects_uses_refreshes_and_is_disconnected(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(database_url, monkeypatch) as c:
        # Discovery: an unauthenticated MCP call says where the resource metadata is, which
        # names IT-Ops as the authorization server.
        r = await c.post(
            f"{MCP}/mcp/", json={}, headers={"Accept": "application/json, text/event-stream"}
        )
        assert r.status_code == 401
        assert "oauth-protected-resource/mcp" in r.headers.get("www-authenticate", "")
        resource = (await c.get(f"{MCP}/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["authorization_servers"] == [f"{MCP}/"]  # the same form as the issuer
        meta = (await c.get(f"{MCP}/.well-known/oauth-authorization-server")).json()
        assert meta["registration_endpoint"] == f"{MCP}/register"
        assert meta["issuer"] == resource["authorization_servers"][0]  # strict clients compare

        # Only known clients can register.
        bad = await c.post(f"{MCP}/register", json={"redirect_uris": ["https://evil.example/cb"]})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"
        reg = await c.post(f"{MCP}/register", json={
            "redirect_uris": [CHATGPT], "client_name": "ChatGPT",
            "token_endpoint_auth_method": "none"})  # fmt: skip
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]

        # Authorize: off to the consent page on the internal site, after signing in.
        verifier, challenge = pkce()
        r = await c.get(f"{MCP}/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CHATGPT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8",
            "scope": "itops"})  # fmt: skip
        assert r.status_code == 302 and r.headers["location"].startswith(f"{BASE}/oauth/consent?")
        consent_url = r.headers["location"]
        assert (await c.get(consent_url)).status_code == 303  # not signed in yet
        await c.get("/auth/login")
        await c.get("/auth/callback?code=abc&state=s1")
        page = (await c.get(consent_url)).text
        assert "Connect ChatGPT to IT-Ops" in page and "via AI" in page and "chatgpt.com" in page

        request_id = parse_qs(urlsplit(consent_url).query)["request"][0]
        allowed = await c.post(
            "/oauth/consent", data={"request_id": request_id, "decision": "allow"}
        )
        target = html.unescape(re.search(r"url=([^'\"]+)", allowed.text)[1])  # type: ignore[index]
        again_allowed = await c.post(
            "/oauth/consent", data={"request_id": request_id, "decision": "allow"}
        )
        assert "url=" not in again_allowed.text  # a second click doesn't issue a second code
        assert target.startswith(CHATGPT)
        query = parse_qs(urlsplit(target).query)
        assert query["state"] == ["st8"]
        code = query["code"][0]

        # Trade the code (PKCE) for tokens; the code works once.
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CHATGPT,
                "client_id": client_id, "code_verifier": verifier}  # fmt: skip
        tokens = (await c.post(f"{MCP}/token", data=form)).json()
        assert tokens["access_token"].startswith("itops_oat_") and tokens["refresh_token"]
        assert (await c.post(f"{MCP}/token", data=form)).status_code == 400

        # The token acts as the person, via AI (like a personal access token).
        bearer = {"Authorization": f"Bearer {tokens['access_token']}"}
        async with httpx.AsyncClient(transport=c._transport, base_url=BASE) as api:  # pyright: ignore[reportPrivateUsage]
            assert (await api.get("/search", params={"q": "x"}, headers=bearer)).status_code == 200
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"}}}  # fmt: skip
            mcp_headers = {**bearer, "Accept": "application/json, text/event-stream"}
            assert (
                await api.post(f"{MCP}/mcp/", json=init, headers=mcp_headers)
            ).status_code == 200
            # The resource is advertised without the slash; Claude posts there. No redirect.
            assert (await api.post(f"{MCP}/mcp", json=init, headers=mcp_headers)).status_code == 200
            # Refresh rotates: the old refresh token stops working.
            refreshed = (await api.post(f"{MCP}/token", data={
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": client_id})).json()  # fmt: skip
            assert refreshed["access_token"] != tokens["access_token"]
            again = await api.post(f"{MCP}/token", data={
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": client_id})  # fmt: skip
            assert again.status_code == 400
            # Two refreshes racing with one token: exactly one wins.
            race = {"grant_type": "refresh_token", "refresh_token": refreshed["refresh_token"],
                    "client_id": client_id}  # fmt: skip
            results = await asyncio.gather(
                api.post(f"{MCP}/token", data=race), api.post(f"{MCP}/token", data=race)
            )
            assert sorted(r.status_code for r in results) == [200, 400]
            refreshed = next(r for r in results if r.status_code == 200).json()

        # The person sees the connection on /account and can disconnect it.
        account = (await c.get("/account")).text
        assert "Connected AI tools" in account and "ChatGPT" in account
        grant = re.search(r"/account/connections/([0-9a-fA-F-]+)/revoke", account)[1]  # type: ignore[index]
        assert (await c.post("/account/connections/not-a-uuid/revoke")).status_code == 404
        await c.post(f"/account/connections/{grant}/revoke")
        new_bearer = {"Authorization": f"Bearer {refreshed['access_token']}"}
        async with httpx.AsyncClient(transport=c._transport, base_url=BASE) as api:  # pyright: ignore[reportPrivateUsage]
            assert (
                await api.get("/search", params={"q": "x"}, headers=new_bearer)
            ).status_code == 401


async def test_public_mcp_host_answers_for_the_mcp_surface_only(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(database_url, monkeypatch) as c:
        for path in (
            "/docs/",
            "/account",
            "/search?q=x",
            "/oauth/consent?request=x",
            "/api/docs/collections",
            "/tokens",
            "/mcpx",
        ):
            assert (await c.get(f"{MCP}{path}")).status_code == 404, path
        assert (await c.get(f"{MCP}/.well-known/oauth-authorization-server")).status_code == 200
        # The internal site is unaffected, unless the request came through Cloudflare.
        assert (await c.get("/health")).status_code == 200
        tunnelled = {"cf-ray": "8f0000000000-ORD"}
        assert (await c.get("/docs/", headers=tunnelled)).status_code == 404
        meta = await c.get("/.well-known/oauth-authorization-server", headers=tunnelled)
        assert meta.status_code == 200


async def test_leavers_lose_their_tokens(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AD says the person is disabled: their tokens stop working and are revoked (checked at
    most hourly while in use, and at every OAuth refresh); AD unreachable keeps access."""
    answers = {"now": DirectoryAnswer("active", ["App-ITOps-Editors"])}

    async def directory(upn: str, groups: list[str]) -> DirectoryAnswer:
        return answers["now"]

    monkeypatch.setattr(identity_module, "DIRECTORY_RETRY_MINUTES", 0)
    async with running(database_url, monkeypatch, directory) as c:
        await c.get("/auth/login")
        await c.get("/auth/callback?code=abc&state=s1")
        made = await c.post("/account/tokens", data={"name": "laptop", "days": "7"})
        token = re.search(r"(itops_[A-Za-z0-9_-]{20,})", made.text)[1]  # type: ignore[index]
        bearer = {"Authorization": f"Bearer {token}"}

        async def search_status() -> int:
            async with httpx.AsyncClient(transport=c._transport, base_url=BASE) as api:  # pyright: ignore[reportPrivateUsage]
                return (await api.get("/search", params={"q": "x"}, headers=bearer)).status_code

        assert await search_status() == 200
        answers["now"] = DirectoryAnswer("error")  # AD can't be reached: what was known stands
        await stale(c)
        assert await search_status() == 200
        answers["now"] = DirectoryAnswer("disabled")  # disabled: refused, and revoked for good
        await stale(c)
        assert await search_status() == 401
        answers["now"] = DirectoryAnswer("active", ["App-ITOps-Editors"])
        await stale(c)
        assert await search_status() == 401


async def stale(c: httpx.AsyncClient) -> None:
    """Pretend every AD check was long ago."""
    app: Any = c._transport.app  # type: ignore[attr-defined]  # pyright: ignore
    async with app.state.runtime.sessionmaker() as s:
        await s.execute(text("UPDATE itops.users SET directory_checked_at = '2000-01-01'"))
        await s.commit()


def test_directory_answers_are_parsed() -> None:
    assert parse("STATUS active\nGROUP App-ITOps-Admins\n") == DirectoryAnswer(
        "active", ["App-ITOps-Admins"]
    )
    assert parse("STATUS disabled\nGROUP x") == DirectoryAnswer("disabled", [])
    assert parse("garbage").status == "error"


async def test_admins_see_and_cut_off_every_ai_connection(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(database_url, monkeypatch) as editor:
        await editor.get("/auth/login")
        await editor.get("/auth/callback?code=abc&state=s1")
        assert (await editor.get("/ai-connections")).status_code == 403
    async with running(database_url, monkeypatch, groups=["App-ITOps-Admins"]) as c:
        await c.get("/auth/login")
        await c.get("/auth/callback?code=abc&state=s1")
        made = await c.post("/account/tokens", data={"name": "Admin probe", "days": "7"})
        token = re.search(r"(itops_[A-Za-z0-9_-]{20,})", made.text)[1]  # type: ignore[index]
        bearer = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=c._transport, base_url=BASE) as api:  # pyright: ignore[reportPrivateUsage]
            assert (await api.get("/ai-connections", headers=bearer)).status_code == 403
        page = (await c.get("/ai-connections")).text
        assert "Admin probe" in page and "Pat Example" in page and "Nothing refused" in page
        token_id = re.search(r"/ai-connections/tokens/(\d+)/revoke", page)[1]  # type: ignore[index]
        await c.post(f"/ai-connections/tokens/{token_id}/revoke")
        async with httpx.AsyncClient(transport=c._transport, base_url=BASE) as api:  # pyright: ignore[reportPrivateUsage]
            r = await api.get(
                "/search", params={"q": "x"}, headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 401
        assert (await c.post("/ai-connections/oauth/not-a-uuid/revoke")).status_code == 404
        trail = html.unescape((await c.get("/audit?module=itops")).text)
        assert "Revoked Pat Example's access token “Admin probe”" in trail


async def test_ad_outages_dont_flood_ad(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """AD unreachable: access stands, and AD isn't asked again for a few minutes; parallel
    requests for one person share one check."""
    calls: list[str] = []

    async def directory(upn: str, groups: list[str]) -> DirectoryAnswer:
        calls.append(upn)
        await asyncio.sleep(0.05)
        return DirectoryAnswer("error")

    async with running(database_url, monkeypatch, directory) as c:
        await c.get("/auth/login")
        await c.get("/auth/callback?code=abc&state=s1")
        made = await c.post("/account/tokens", data={"name": "probe", "days": "7"})
        token = re.search(r"(itops_[A-Za-z0-9_-]{20,})", made.text)[1]  # type: ignore[index]
        bearer = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=c._transport, base_url=BASE) as api:  # pyright: ignore[reportPrivateUsage]
            results = await asyncio.gather(
                *(api.get("/search", params={"q": "x"}, headers=bearer) for _ in range(5))
            )
            assert all(r.status_code == 200 for r in results)
            assert len(calls) == 1  # one shared check
            await stale(c)
            assert (await api.get("/search", params={"q": "x"}, headers=bearer)).status_code == 200
            assert len(calls) == 1  # backing off after the failure
