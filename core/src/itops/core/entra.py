"""Entra ID sign-in: the sign-in routes and the gate every request passes (entra mode).

* Browsers: OpenID Connect authorization-code flow via MSAL (it checks state, nonce and
  the id token). The session is a signed, HTTP-only cookie holding who you are and your
  roles (from your groups at sign-in), valid ``ITOPS_SESSION_HOURS``.
* Scripts and AI tools: ``Authorization: Bearer <personal access token>`` on ``/api``;
  MCP (``/mcp``) checks tokens in the MCP SDK itself. Tokens act *via AI*.
* Everything else is refused: pages redirect to sign-in, API calls get 401.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from itops.core import auth
from itops.core.apps import APP_TOKEN_PREFIX, AppTokens
from itops.core.config import Settings
from itops.core.identity import Identity, User, group_names, real_groups, roles_from_groups
from itops.core.theme import parse_appearance

if TYPE_CHECKING:
    from itops.core.sites import Sites

# The MCP surface of ADR 0013: the endpoint, the OAuth endpoints and their discovery
# documents. The only paths that answer on the public MCP host.
MCP_PATHS = (
    "/mcp", "/authorize", "/token", "/register", "/revoke",
    "/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server",
)  # fmt: skip
PUBLIC_PREFIXES = ("/health", "/static/", "/theme/", "/auth/", "/favicon.ico",
                   "/manifest.webmanifest")  # fmt: skip


def is_mcp_path(path: str) -> bool:
    """``/mcp``, ``/mcp/...``, ``/token`` ... exactly: not ``/tokens`` or ``/mcpx``."""
    return any(path == p or path.startswith(p + "/") for p in MCP_PATHS)


SCOPES = ["User.Read"]

# MSAL recommends response_mode=form_post. We deliberately use the default (query):
# Entra would POST the callback cross-site, and our SameSite=lax session cookie (our
# CSRF defence) is not sent on cross-site POSTs, so the sign-in flow would be lost.
# PKCE, state and nonce (all checked by MSAL) protect the code in the query string.
warnings.filterwarnings("ignore", message=r"response_mode='form_post' is recommended")

# (request, title, detail, status=403) -> a standalone page (the user may not be signed in)
Message = Callable[..., Response]


def _safe_next(target: str | None) -> str:
    """Only same-site paths: never redirect to another host after sign-in."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


class EntraAuth:
    def __init__(
        self,
        settings: Settings,
        identity: Identity,
        message: Message,
        base_url: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.identity = identity
        self.message = message
        # The address of the site being signed in to (ADR 0027), IT-Ops' own by default.
        self.base_url = base_url or (lambda: settings.public_url.rstrip("/"))
        self._msal: Any = None

    @property
    def redirect_uri(self) -> str:
        return self.base_url() + "/auth/callback"

    def _app(self) -> Any:
        if self._msal is None:
            import msal

            self._msal = msal.ConfidentialClientApplication(
                self.settings.entra_client_id,
                authority=f"https://login.microsoftonline.com/{self.settings.entra_tenant_id}",
                client_credential=self.settings.entra_client_secret.get_secret_value(),
            )
        return self._msal

    # ---- routes ----------------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        flow = await asyncio.to_thread(
            lambda: self._app().initiate_auth_code_flow(SCOPES, redirect_uri=self.redirect_uri)
        )
        request.session["flow"] = flow
        request.session["next"] = _safe_next(request.query_params.get("next"))
        return _external_redirect(flow["auth_uri"])

    async def callback(self, request: Request) -> Response:
        flow = request.session.pop("flow", None)
        if not flow:
            return RedirectResponse("/auth/login", status_code=303)
        query = dict(request.query_params)
        result: dict[str, Any] = await asyncio.to_thread(
            lambda: self._app().acquire_token_by_auth_code_flow(flow, query)
        )
        if "error" in result:
            return self.message(
                request,
                "Sign-in didn't complete",
                str(result.get("error_description") or result["error"]).splitlines()[0],
            )
        claims: dict[str, Any] = result.get("id_token_claims") or {}
        raw_groups: list[Any] = claims.get("groups") or []
        groups = [str(g) for g in raw_groups]
        overage: dict[str, Any] = claims.get("_claim_names") or {}
        upn = str(claims.get("preferred_username", ""))
        asked_ad = False
        if not groups and "groups" in overage and self.identity.directory is not None:
            # In too many groups for the token (Entra's limit is 200): ask AD about the
            # ones that matter instead (role groups and restricted areas', ADR 0012).
            answer = await self.identity.directory(upn, await self.identity.directory_groups())
            groups = answer.groups
            asked_ad = True
        roles = roles_from_groups(groups, self.settings.role_groups)
        if roles and not asked_ad and self.identity.directory is not None:
            # The token carries only groups synced to Entra *and* assigned to the app, so a
            # permission group (ADR 0014) or an area's group (ADR 0012) missing either step
            # silently didn't count (Contracts, 2026-10-14). Ask AD about those directly;
            # if it can't answer, the token's groups stand.
            answer = await self.identity.directory(upn, await self.identity.directory_groups())
            if answer.status == "active":
                groups = [*groups, *answer.groups]
        if not roles:
            await self.identity.record_refused(_user(claims, ()), groups)
            return self.message(
                request,
                "You're signed in, but not set up for IT-Ops yet",
                "Ask an IT-Ops admin to add you to one of the IT-Ops groups "
                f"({', '.join(real_groups(self.settings.role_groups))}).",
            )
        user = replace(_user(claims, tuple(roles)), groups=group_names(groups))
        appearance = await self.identity.record_sign_in(user)
        request.session["user"] = {
            "id": user.id,
            "upn": user.upn,
            "name": user.display_name,
            "roles": list(user.roles),
        }
        request.session["appearance"] = parse_appearance(appearance)
        return RedirectResponse(request.session.pop("next", "/"), status_code=303)

    async def logout(self, request: Request) -> Response:
        request.session.clear()
        return self.message(request, "Signed out", "You're signed out of IT-Ops.", status=200)


def _user(claims: dict[str, Any], roles: tuple[str, ...]) -> User:
    return User(
        id=str(claims.get("oid", "")),
        upn=str(claims.get("preferred_username", "")),
        display_name=str(claims.get("name", "") or claims.get("preferred_username", "")),
        roles=roles,
    )


def _external_redirect(url: str, label: str = "Continue to Microsoft sign-in") -> Response:
    """Send the browser to another site (Microsoft sign-in, an AI client) with a page, not a
    Location header: IIS ARR rewrites the host of every Location header it proxies (a
    server-wide setting other sites rely on), which turned login.microsoftonline.com into our
    own name. Page content passes through untouched."""
    from html import escape

    target = escape(url, quote=True)
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta http-equiv='refresh' content='0;url={target}'><title>Signing in…</title>"
        f"</head><body><p><a href='{target}'>{escape(label)}</a></p></body></html>"
    )
    return HTMLResponse(
        body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )


external_redirect = _external_redirect

LOOPBACK = ("127.0.0.1", "::1")


def public_host(scope: Scope, request: Request) -> str:
    """The name the browser used. Behind the IIS reverse proxy (same machine) the Host
    header is the loopback address and the real name arrives as X-Forwarded-Host; that
    header is only trusted from loopback, so nobody can spoof it from outside."""
    client = scope.get("client")
    forwarded = request.headers.get("x-forwarded-host", "")
    if forwarded and client and client[0] in LOOPBACK:
        return forwarded.split(",")[0].strip()
    return request.headers.get("host", "")


def session_user(scope: Scope) -> User | None:
    session: dict[str, Any] = scope.get("session") or {}
    raw: object = session.get("user")
    if not isinstance(raw, dict):
        return None
    d = cast(dict[str, Any], raw)
    roles: list[Any] = d.get("roles") or []
    return User(
        id=str(d.get("id", "")),
        upn=str(d.get("upn", "")),
        display_name=str(d.get("name", "")),
        roles=tuple(str(r) for r in roles),
    )


class AuthGate:
    """Pure ASGI middleware (so the principal is set in the same task as the handler).
    Must sit inside SessionMiddleware."""

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        identity: Identity,
        apps: AppTokens | None = None,
        sites: Sites | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.identity = identity
        self.apps = apps
        self.public_host = urlsplit(settings.public_url).netloc
        self.site_hosts = sites.hosts if sites else set[str]()
        mcp_host = urlsplit(settings.mcp_public_url).netloc if settings.mcp_public_url else ""
        # Only a separate public host is restricted (the same host would lock out the site).
        self.mcp_host = mcp_host if mcp_host != self.public_host else ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.settings.auth_mode != "entra":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        request = Request(scope)
        host = public_host(scope, request)
        full = path + (f"?{scope['query_string'].decode()}" if scope["query_string"] else "")
        other_host = host and host != self.public_host and host.lower() not in self.site_hosts
        # With a public MCP host, anything that came through Cloudflare (it stamps every
        # request) is public too, whatever name it asked for.
        through_cloudflare = bool(self.settings.mcp_public_url) and "cf-ray" in request.headers
        public = (self.mcp_host and host == self.mcp_host) or through_cloudflare
        if public and not is_mcp_path(path):
            # The public MCP host answers for the MCP surface only (ADR 0013).
            await JSONResponse({"detail": "Not found"}, 404)(scope, receive, send)
            return
        # /health is checked on loopback (deploys, monitoring), so it answers on any name.
        exempt = is_mcp_path(path) or path.startswith("/health")
        if request.method == "GET" and other_host and not exempt:
            # One canonical address, so the session cookie and sign-in redirect agree.
            base = self.settings.public_url.rstrip("/")
            await RedirectResponse(base + full)(scope, receive, send)
            return
        if path.startswith(PUBLIC_PREFIXES) or is_mcp_path(path):
            await self.app(scope, receive, send)
            return

        principal: auth.Principal | None = None
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token.startswith(APP_TOKEN_PREFIX) and self.apps is not None:
            # An app acting as itself (ADR 0021): the REST API and the files its reads link
            # to (checked like anyone's), never the web pages.
            principal = await self.apps.principal_for(token)
            files = path.startswith("/files/") and request.method == "GET"
            if principal is None or not (path.startswith("/api/") or files):
                denied = JSONResponse({"detail": "Invalid or expired token"}, 401)
                await denied(scope, receive, send)
                return
        elif header.lower().startswith("bearer "):
            user = await self.identity.user_for_token(token)
            if user is None:
                denied = JSONResponse({"detail": "Invalid or expired token"}, 401)
                await denied(scope, receive, send)
                return
            principal = self.identity.principal(user, via_ai=True, client="api")
        elif (user := session_user(scope)) is not None:
            # The cookie holds who and their roles; their groups come from the user row.
            principal = self.identity.principal(await self.identity.with_groups(user))

        if principal is None:
            if path.startswith("/api/") or request.method != "GET":
                response: Response = JSONResponse({"detail": "Sign in required"}, 401)
            else:
                response = RedirectResponse(f"/auth/login?next={quote(full)}", status_code=303)
            await response(scope, receive, send)
            return

        auth.set_current(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            auth.set_current(None)
