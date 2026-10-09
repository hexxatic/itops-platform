"""OAuth for MCP clients that can't send a personal access token, like ChatGPT (ADR 0013).

IT-Ops is the authorization server (the MCP SDK's handlers do the protocol: metadata,
``/register``, ``/authorize``, ``/token``, ``/revoke``); this module is the provider behind
them, with IT-Ops' own storage. Signing in is delegated to Entra: ``authorize`` sends the
browser to the consent page on the internal site (``/oauth/consent``), which needs the usual
sign-in and says who wants to act as you, *via AI*. Allow issues a one-time code; the client
trades it (with PKCE) for an access token (1 hour) and a refresh token (30 days, rotated;
every refresh checks the person's AD account is still allowed, directory.py).

Tokens act exactly like personal access tokens (identity.py resolves both): the user's
roles, always via AI, listed on /account and revocable there. Only clients whose redirect
URIs are on the allow-list can register. Codes and tokens are stored as hashes only.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core.audit import AuditLog
from itops.core.identity import User, hash_token

ACCESS_PREFIX = "itops_oat_"
REFRESH_PREFIX = "itops_ort_"
SCOPE = "itops"
ACCESS_SECONDS = 3600
REFRESH_DAYS = 30  # then connect again (shorter than personal access tokens)
CODE_SECONDS = 300
REQUEST_SECONDS = 900  # time to get through sign-in and consent
CONSENT_PATH = "/oauth/consent"
PURGE_SECONDS = 3600  # sweep expired requests, codes, tokens and unused clients this often


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _ts(value: dt.datetime) -> int:
    return int(value.replace(tzinfo=dt.UTC).timestamp())


def redirect_allowed(uri: str, hosts: list[str]) -> bool:
    """A redirect URI on an allowed host, over https (http only for localhost)."""
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (local and parts.scheme == "http"):
        return False
    return any(host == h or host.endswith("." + h) for h in (x.lower() for x in hosts))


class OAuthProvider:
    """The MCP SDK's ``OAuthAuthorizationServerProvider``, backed by the IT-Ops database."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        site_url: str,
        redirect_hosts: list[str],
        audit: AuditLog | None = None,
        still_allowed: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._maker = sessionmaker
        self._audit = audit or AuditLog()
        self._still_allowed = still_allowed  # a fresh AD check at every refresh
        self._site_url = site_url.rstrip("/")  # the internal site, where consent happens
        self._redirect_hosts = redirect_hosts
        self._purged_at = float("-inf")

    # -- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._maker() as s:
            info = (
                await s.execute(
                    text("SELECT info FROM itops.oauth_clients WHERE client_id = :c"),
                    {"c": client_id},
                )
            ).scalar()
        return OAuthClientInformationFull.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(redirect_allowed(u, self._redirect_hosts) for u in uris):
            raise RegistrationError(
                "invalid_redirect_uri",
                "IT-Ops only accepts known AI clients (ChatGPT, Claude, local desktop tools).",
            )
        async with self._maker() as s:
            await s.execute(
                text("INSERT INTO itops.oauth_clients (client_id, info) VALUES (:c, :i)"),
                {"c": client_info.client_id, "i": client_info.model_dump_json()},
            )
            await s.commit()

    async def purge_expired(self, *, now: bool = False) -> None:
        """Delete what can never be used again: expired sign-in requests and codes, tokens a
        month past expiry (until then the rate limiter still knows a stale token from a
        made-up one), and registered clients that never connected (a day on). At most
                hourly unless ``now``; registration and sign-in requests come from the internet."""
        if not now and time.monotonic() - self._purged_at < PURGE_SECONDS:
            return
        self._purged_at = time.monotonic()
        cutoff = _now() - dt.timedelta(days=1)
        async with self._maker() as s:
            await s.execute(text("DELETE FROM itops.oauth_requests WHERE expires_at < :n"),
                            {"n": _now()})  # fmt: skip
            await s.execute(text("DELETE FROM itops.oauth_codes WHERE expires_at < :n"),
                            {"n": _now()})  # fmt: skip
            await s.execute(text("DELETE FROM itops.oauth_tokens WHERE expires_at < :c"),
                            {"c": _now() - dt.timedelta(days=30)})  # fmt: skip
            await s.execute(
                text(
                    "DELETE c FROM itops.oauth_clients c WHERE c.created_at < :c"
                    " AND NOT EXISTS (SELECT 1 FROM itops.oauth_tokens t"
                    "                 WHERE t.client_id = c.client_id)"
                    " AND NOT EXISTS (SELECT 1 FROM itops.oauth_codes o"
                    "                 WHERE o.client_id = c.client_id)"
                    " AND NOT EXISTS (SELECT 1 FROM itops.oauth_requests r"
                    "                 WHERE r.client_id = c.client_id)"
                ),
                {"c": cutoff},
            )
            await s.commit()

    # -- the browser's trip through sign-in and consent

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        await self.purge_expired()
        request_id = secrets.token_urlsafe(32)
        async with self._maker() as s:
            await s.execute(
                text(
                    "INSERT INTO itops.oauth_requests (id_hash, client_id, params, expires_at)"
                    " VALUES (:h, :c, :p, :e)"
                ),
                {
                    "h": hash_token(request_id),
                    "c": client.client_id,
                    "p": params.model_dump_json(),
                    "e": _now() + dt.timedelta(seconds=REQUEST_SECONDS),
                },
            )
            await s.commit()
        return f"{self._site_url}{CONSENT_PATH}?request={request_id}"

    async def pending(
        self, request_id: str
    ) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        """The authorization request behind a consent page, if it's still valid."""
        async with self._maker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT client_id, params FROM itops.oauth_requests"
                        " WHERE id_hash = :h AND expires_at > :n"
                    ),
                    {"h": hash_token(request_id), "n": _now()},
                )
            ).first()
        if row is None:
            return None
        client = await self.get_client(row[0])
        if client is None:
            return None
        return client, AuthorizationParams.model_validate_json(row[1])

    async def decide(self, request_id: str, user: User | None, *, allow: bool) -> str:
        """Finish a consent: the URL to send the browser back to the client with (a code,
        or ``access_denied``). The request can't be used twice."""
        found = await self.pending(request_id)
        if found is None:
            raise ValueError("This sign-in request has expired. Start again from the AI tool.")
        client, params = found
        async with self._maker() as s:
            used = await s.execute(
                text("DELETE FROM itops.oauth_requests WHERE id_hash = :h"),
                {"h": hash_token(request_id)},
            )
            if not getattr(used, "rowcount", 0):  # a second click: the first one won
                raise ValueError("This sign-in request was already used. Start again from the"
                                 " AI tool if it didn't connect.")  # fmt: skip
            redirect = str(params.redirect_uri)
            if not allow or user is None:
                await s.commit()
                return construct_redirect_uri(redirect, error="access_denied", state=params.state)
            code = secrets.token_urlsafe(32)
            await s.execute(
                text(
                    "INSERT INTO itops.oauth_codes"
                    " (code_hash, client_id, user_id, params, expires_at)"
                    " VALUES (:h, :c, :u, :p, :e)"
                ),
                {
                    "h": hash_token(code), "c": client.client_id, "u": user.id,
                    "p": params.model_dump_json(),
                    "e": _now() + dt.timedelta(seconds=CODE_SECONDS),
                },
            )  # fmt: skip
            name = client.client_name or "an AI tool"
            await self._audit.record(
                s, module="itops", action="oauth.connected", entity_kind="user",
                entity_id=user.id, title=user.display_name, summary=f"Connected {name}",
                details={"client": name, "redirect": str(params.redirect_uri)},
            )  # fmt: skip
            await s.commit()
        return construct_redirect_uri(redirect, code=code, state=params.state)

    # -- codes and tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with self._maker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT user_id, params, expires_at FROM itops.oauth_codes"
                        " WHERE code_hash = :h AND client_id = :c"
                    ),
                    {"h": hash_token(authorization_code), "c": client.client_id},
                )
            ).first()
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row[1])
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [SCOPE],
            expires_at=float(_ts(row[2])),
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=row[0],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with self._maker() as s:
            used = await s.execute(
                text("DELETE FROM itops.oauth_codes WHERE code_hash = :h"),
                {"h": hash_token(authorization_code.code)},
            )
            if not getattr(used, "rowcount", 0):  # already traded: codes are single-use
                raise TokenError("invalid_grant", "authorization code already used")
            token = await self._issue(
                s, grant_id=uuid.uuid4(), client_id=client.client_id,
                user_id=str(authorization_code.subject), scopes=authorization_code.scopes,
                resource=authorization_code.resource,
            )  # fmt: skip
            await s.commit()
        return token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await self._token_row(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=_ts(row["expires_at"]), resource=row["resource"], subject=row["user_id"],
        )  # fmt: skip

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate: the old refresh token stops working; the grant carries on."""
        row = await self._token_row(refresh_token.token, "refresh")
        if row is None:
            raise TokenError("invalid_grant", "refresh token expired or revoked")
        if self._still_allowed is not None and not await self._still_allowed(row["user_id"]):
            raise TokenError("invalid_grant", "this account no longer has access to IT-Ops")
        async with self._maker() as s:
            rotated = await s.execute(
                text(
                    "UPDATE itops.oauth_tokens SET revoked_at = :n"
                    " WHERE token_hash = :h AND revoked_at IS NULL"
                ),
                {"n": _now(), "h": hash_token(refresh_token.token)},
            )
            if not getattr(rotated, "rowcount", 0):  # a concurrent refresh got there first
                raise TokenError("invalid_grant", "refresh token already used")
            token = await self._issue(
                s, grant_id=row["grant_id"], client_id=client.client_id, user_id=row["user_id"],
                scopes=scopes or row["scopes"], resource=row["resource"],
            )  # fmt: skip
            await s.commit()
        return token

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """Enterprise identity-assertion grants (SEP-990) aren't offered."""
        raise TokenError("unsupported_grant_type", "identity assertions aren't accepted")

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self._token_row(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token, client_id=row["client_id"], scopes=row["scopes"],
            expires_at=_ts(row["expires_at"]), resource=row["resource"], subject=row["user_id"],
        )  # fmt: skip

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoking either token ends the whole grant (the client has to ask again)."""
        async with self._maker() as s:
            await s.execute(
                text(
                    "UPDATE itops.oauth_tokens SET revoked_at = :n WHERE revoked_at IS NULL"
                    " AND grant_id ="
                    " (SELECT grant_id FROM itops.oauth_tokens WHERE token_hash = :h)"
                ),
                {"n": _now(), "h": hash_token(token.token)},
            )
            await s.commit()

    async def _issue(
        self, s: AsyncSession, *, grant_id: Any, client_id: str, user_id: str,
        scopes: list[str], resource: str | None,
    ) -> OAuthToken:  # fmt: skip
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        now = _now()
        for value, kind, life in (
            (access, "access", dt.timedelta(seconds=ACCESS_SECONDS)),
            (refresh, "refresh", dt.timedelta(days=REFRESH_DAYS)),
        ):
            await s.execute(
                text(
                    "INSERT INTO itops.oauth_tokens (grant_id, token_hash, kind, client_id,"
                    " user_id, scopes, resource, expires_at)"
                    " VALUES (:g, :h, :k, :c, :u, :s, :r, :e)"
                ),
                {
                    "g": str(grant_id), "h": hash_token(value), "k": kind, "c": client_id,
                    "u": user_id, "s": " ".join(scopes), "r": resource, "e": now + life,
                },
            )  # fmt: skip
        return OAuthToken(
            access_token=access, expires_in=ACCESS_SECONDS, refresh_token=refresh,
            scope=" ".join(scopes),
        )  # fmt: skip

    async def _token_row(self, token: str, kind: str) -> dict[str, Any] | None:
        async with self._maker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT grant_id, client_id, user_id, scopes, resource, expires_at"
                        " FROM itops.oauth_tokens WHERE token_hash = :h AND kind = :k"
                        " AND revoked_at IS NULL AND expires_at > :n"
                    ),
                    {"h": hash_token(token), "k": kind, "n": _now()},
                )
            ).first()
        if row is None:
            return None
        return {
            "grant_id": row[0], "client_id": row[1], "user_id": row[2],
            "scopes": (row[3] or SCOPE).split(), "resource": row[4], "expires_at": row[5],
        }  # fmt: skip

    # -- the user's connections (/account)

    async def connections(self, user_id: str) -> list[dict[str, Any]]:
        """Each still-valid grant: the client's name, when it was connected and last used."""
        async with self._maker() as s:
            rows = await s.execute(
                text(
                    "SELECT t.grant_id, MIN(c.info), MIN(t.created_at), MAX(t.last_used_at)"
                    " FROM itops.oauth_tokens t JOIN itops.oauth_clients c"
                    "   ON c.client_id = t.client_id"
                    " WHERE t.user_id = :u AND t.revoked_at IS NULL AND t.expires_at > :n"
                    "   AND t.kind = 'refresh'"
                    " GROUP BY t.grant_id ORDER BY MIN(t.created_at) DESC"
                ),
                {"u": user_id, "n": _now()},
            )
            out: list[dict[str, Any]] = []
            for grant_id, info, created, used in rows.all():
                client = json.loads(info or "{}")
                first_redirect = str((client.get("redirect_uris") or [""])[0])
                out.append({
                    "grant_id": str(grant_id),
                    "client": str(client.get("client_name") or "An AI tool"),
                    "redirect_host": urlsplit(first_redirect).hostname or "",
                    "created": created, "last_used": used,
                })  # fmt: skip
            return out

    async def all_connections(self) -> list[dict[str, Any]]:
        """Everyone's connected AI tools, for admins (/ai-connections)."""
        async with self._maker() as s:
            rows = await s.execute(
                text(
                    "SELECT t.grant_id, MIN(c.info), MIN(u.display_name), MIN(u.upn),"
                    " MIN(t.created_at), MAX(t.last_used_at)"
                    " FROM itops.oauth_tokens t"
                    " JOIN itops.oauth_clients c ON c.client_id = t.client_id"
                    " JOIN itops.users u ON u.id = t.user_id"
                    " WHERE t.revoked_at IS NULL AND t.expires_at > :n AND t.kind = 'refresh'"
                    " GROUP BY t.grant_id"
                    " ORDER BY COALESCE(MAX(t.last_used_at), MIN(t.created_at)) DESC"
                ),
                {"n": _now()},
            )
            out: list[dict[str, Any]] = []
            for grant_id, info, person, upn, created, used in rows.all():
                client = json.loads(info or "{}")
                first_redirect = str((client.get("redirect_uris") or [""])[0])
                out.append({
                    "grant_id": str(grant_id), "person": person, "upn": upn,
                    "client": str(client.get("client_name") or "An AI tool"),
                    "redirect_host": urlsplit(first_redirect).hostname or "",
                    "created": created, "last_used": used,
                })  # fmt: skip
            return out

    async def revoke_any_grant(self, grant_id: str) -> bool:
        """An admin disconnects someone's AI tool; the audit trail names the admin."""
        async with self._maker() as s:
            owner = (
                await s.execute(
                    text("SELECT TOP 1 user_id FROM itops.oauth_tokens WHERE grant_id = :g"),
                    {"g": grant_id},
                )
            ).scalar()
        return bool(owner) and await self.revoke_grant(str(owner), grant_id)

    async def revoke_grant(self, user_id: str, grant_id: str) -> bool:
        async with self._maker() as s:
            result = await s.execute(
                text(
                    "UPDATE itops.oauth_tokens SET revoked_at = :n WHERE user_id = :u"
                    " AND grant_id = :g AND revoked_at IS NULL"
                ),
                {"n": _now(), "u": user_id, "g": grant_id},
            )
            revoked = bool(getattr(result, "rowcount", 0))
            if revoked:
                await self._audit.record(
                    s, module="itops", action="oauth.disconnected", entity_kind="user",
                    entity_id=user_id, title=user_id, summary="Disconnected an AI tool",
                    details={"grant": grant_id},
                )  # fmt: skip
            await s.commit()
            return revoked
