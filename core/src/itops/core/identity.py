"""People, roles and personal access tokens (Entra sign-in mode).

**Roles** come from on-prem AD groups synced to Entra (design §7): Reader < Editor <
Publisher < Admin, each including the ones before it. A role grants module permissions
by their **last word**, so every module fits without wiring:

    reader    -> *.read
    editor    -> *.read, *.write
    publisher -> *.read, *.write, *.publish
    admin     -> everything

**Personal access tokens** let a person connect an AI tool or script. A token acts as
its owner *via AI* (so human-only permissions like ``docs.publish`` never apply), with
the owner's roles as AD last reported them (re-checked hourly while tokens are in use).
Only a SHA-256 hash is stored; the token itself is shown once. Tokens expire (max 90
days) and can be revoked.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.auth.provider import AccessToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core.audit import AuditLog
from itops.core.auth import ALL, Principal
from itops.core.directory import DirectoryCheck

Role = Literal["reader", "editor", "publisher", "admin"]
ROLES: tuple[Role, ...] = ("reader", "editor", "publisher", "admin")
ROLE_VERBS: dict[Role, frozenset[str]] = {
    "reader": frozenset({"read"}),
    "editor": frozenset({"read", "write"}),
    "publisher": frozenset({"read", "write", "publish"}),
    "admin": frozenset(),  # everything, see permissions_for
}
TOKEN_PREFIX = "itops_"
MAX_TOKEN_DAYS = 90


def roles_from_groups(groups: Iterable[str], role_groups: dict[str, str]) -> list[Role]:
    """Group names from the token (``App-ITOps-Editors`` or ``DOMAIN\\App-...``) -> roles."""
    have = {g.split("\\")[-1].lower() for g in groups}
    return [r for r in ROLES if role_groups.get(r, "").lower() in have]


def top_role(roles: Iterable[str]) -> Role | None:
    held = set(roles)
    for r in reversed(ROLES):
        if r in held:
            return r
    return None


def permissions_for(roles: Iterable[str], declared: Iterable[str]) -> frozenset[str]:
    """Expand roles into concrete permissions from every loaded module's manifest."""
    top = top_role(roles)
    if top is None:
        return frozenset()
    if top == "admin":
        return frozenset({ALL})
    verbs = ROLE_VERBS[top]
    return frozenset(p for p in declared if p.rsplit(".", 1)[-1] in verbs)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


hash_token = _hash  # stored form of a token or one-time code (personal and OAuth)
OAUTH_ACCESS_PREFIX = "itops_oat_"  # OAuth access tokens (ADR 0013; oauth.py issues them)
DIRECTORY_CHECK_MINUTES = 60  # how stale a token owner's AD check may be (directory.py)
DIRECTORY_RETRY_MINUTES = 5  # after AD couldn't be reached, wait this long before asking again


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class User:
    id: str  # Entra object id (oid)
    upn: str
    display_name: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class TokenInfo:
    id: int
    name: str
    prefix: str
    created_at: dt.datetime
    expires_at: dt.datetime
    last_used_at: dt.datetime | None


class Identity:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], audit: AuditLog | None = None
    ) -> None:
        self._sessionmaker = sessionmaker
        self._audit = audit or AuditLog()
        self._declared: frozenset[str] = frozenset()
        # Token owners re-checked against AD (directory.py); None: not checked (dev, tests).
        self.directory: DirectoryCheck | None = None
        self.role_groups: dict[str, str] = {}
        self._directory_checks: dict[str, asyncio.Future[User | None]] = {}
        self._directory_failed: dict[str, float] = {}  # user id -> when AD last failed

    def set_declared_permissions(self, permissions: Iterable[str]) -> None:
        self._declared = frozenset(permissions)

    def principal(self, user: User, *, via_ai: bool = False, client: str = "web") -> Principal:
        return Principal(
            user=user.upn,
            display_name=user.display_name,
            permissions=permissions_for(user.roles, self._declared),
            via_ai=via_ai,
            client=client,
        )

    async def record_sign_in(self, user: User) -> str:
        """Create or refresh the user row (roles as of this sign-in). Returns their saved
        appearance (core/theme.py)."""
        async with self._sessionmaker() as s:
            result = await s.execute(
                text(
                    "MERGE itops.users AS t USING (SELECT :id AS id) AS src ON t.id = src.id"
                    " WHEN MATCHED THEN UPDATE SET upn = :upn, display_name = :name,"
                    "   roles = :roles, last_sign_in = sysutcdatetime()"
                    " WHEN NOT MATCHED THEN INSERT (id, upn, display_name, roles, last_sign_in)"
                    "   VALUES (:id, :upn, :name, :roles, sysutcdatetime())"
                    " OUTPUT inserted.appearance;"
                ),
                {
                    "id": user.id,
                    "upn": user.upn,
                    "name": user.display_name,
                    "roles": ",".join(user.roles),
                },
            )
            await self._record(
                s, user, "user.signed_in", f"Signed in as {top_role(user.roles)}",
                roles=list(user.roles),
            )  # fmt: skip
            appearance = str(result.scalar_one())
            await s.commit()
            return appearance

    async def set_appearance(self, user: User, appearance: str) -> None:
        """Light, dark or auto, kept with the user so it follows them to any browser."""
        async with self._sessionmaker() as s:
            await s.execute(
                text("UPDATE itops.users SET appearance = :a WHERE id = :id"),
                {"a": appearance, "id": user.id},
            )
            await s.commit()

    async def record_refused(self, user: User, groups: list[str]) -> None:
        """Signed in with Entra, but in none of the role groups."""
        async with self._sessionmaker() as s:
            await self._record(
                s, user, "user.sign_in_refused", "Sign-in refused: not in an IT-Ops group",
                groups=groups[:50],
            )  # fmt: skip
            await s.commit()

    async def _record(
        self, s: AsyncSession, user: User, action: str, summary: str, **details: Any
    ) -> None:
        await self._audit.record(
            s,
            module="itops",
            action=action,
            entity_kind="user",
            entity_id=user.id,
            title=user.display_name,
            summary=summary,
            details=details or None,
            actor=Principal(user.upn, user.display_name, frozenset()),
        )

    # ---- tokens --------------------------------------------------------------------

    async def create_token(self, user: User, name: str, days: int = MAX_TOKEN_DAYS) -> str:
        """Returns the new token. It is never stored or shown again."""
        name = " ".join(name.split())[:100] or "AI tool"
        days = max(1, min(days, MAX_TOKEN_DAYS))
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        async with self._sessionmaker() as s:
            await s.execute(
                text(
                    "INSERT INTO itops.api_tokens (user_id, name, token_hash, prefix, expires_at)"
                    " VALUES (:u, :n, :h, :p, :e)"
                ),
                {
                    "u": user.id,
                    "n": name,
                    "h": _hash(token),
                    "p": token[: len(TOKEN_PREFIX) + 6],
                    "e": _now() + dt.timedelta(days=days),
                },
            )
            await self._record(
                s, user, "token.created", f"Created access token “{name}” ({days} days)",
                name=name, prefix=token[: len(TOKEN_PREFIX) + 6], days=days,
            )  # fmt: skip
            await s.commit()
        return token

    async def tokens(self, user_id: str) -> list[TokenInfo]:
        async with self._sessionmaker() as s:
            rows = await s.execute(
                text(
                    "SELECT id, name, prefix, created_at, expires_at, last_used_at"
                    " FROM itops.api_tokens WHERE user_id = :u AND revoked_at IS NULL"
                    " AND expires_at > sysutcdatetime() ORDER BY created_at DESC"
                ),
                {"u": user_id},
            )
            return [TokenInfo(*r) for r in rows.all()]

    async def revoke_token(self, user: User, token_id: int) -> bool:
        async with self._sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE itops.api_tokens SET revoked_at = sysutcdatetime()"
                        " OUTPUT inserted.name, inserted.prefix"
                        " WHERE id = :i AND user_id = :u AND revoked_at IS NULL"
                    ),
                    {"i": token_id, "u": user.id},
                )
            ).first()
            if row is None:
                return False
            await self._record(
                s, user, "token.revoked", f"Revoked access token “{row[0]}”",
                name=row[0], prefix=row[1],
            )  # fmt: skip
            await s.commit()
            return True

    async def all_tokens(self) -> list[dict[str, Any]]:
        """Everyone's live personal access tokens, for admins (/ai-connections)."""
        async with self._sessionmaker() as s:
            rows = await s.execute(
                text(
                    "SELECT t.id, u.display_name, u.upn, t.name, t.prefix, t.created_at,"
                    " t.expires_at, t.last_used_at FROM itops.api_tokens t"
                    " JOIN itops.users u ON u.id = t.user_id"
                    " WHERE t.revoked_at IS NULL AND t.expires_at > sysutcdatetime()"
                    " ORDER BY COALESCE(t.last_used_at, t.created_at) DESC"
                )
            )
            keys = ("id", "person", "upn", "name", "prefix", "created", "expires", "last_used")
            return [dict(zip(keys, r, strict=True)) for r in rows.all()]

    async def revoke_any_token(self, token_id: int) -> bool:
        """An admin revokes someone's token; the audit trail names the admin."""
        async with self._sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE t SET revoked_at = sysutcdatetime()"
                        " OUTPUT inserted.name, inserted.user_id, u.display_name"
                        " FROM itops.api_tokens t JOIN itops.users u ON u.id = t.user_id"
                        " WHERE t.id = :i AND t.revoked_at IS NULL"
                    ),
                    {"i": token_id},
                )
            ).first()
            if row is None:
                return False
            await self._audit.record(
                s, module="itops", action="token.revoked", entity_kind="user",
                entity_id=row[1], title=row[2],
                summary=f"Revoked {row[2]}'s access token “{row[0]}”",
                details={"name": row[0]},
            )  # fmt: skip
            await s.commit()
            return True

    async def user_for_token(self, token: str) -> User | None:
        """The user behind a personal access token or an OAuth access token (ADR 0013)."""
        if token.startswith(OAUTH_ACCESS_PREFIX):
            return await self._user_for_oauth_token(token)
        if not token.startswith(TOKEN_PREFIX):
            return None
        async with self._sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT t.id, u.id, u.upn, u.display_name, u.roles, u.directory_checked_at"
                        " FROM itops.api_tokens t JOIN itops.users u ON u.id = t.user_id"
                        " WHERE t.token_hash = :h AND t.revoked_at IS NULL"
                        " AND t.expires_at > sysutcdatetime()"
                    ),
                    {"h": _hash(token)},
                )
            ).first()
            if row is None:
                return None
            await s.execute(
                text(
                    "UPDATE itops.api_tokens SET last_used_at = sysutcdatetime() WHERE id = :i"
                    " AND (last_used_at IS NULL"
                    "      OR last_used_at < DATEADD(minute, -5, sysutcdatetime()))"
                ),
                {"i": row[0]},
            )
            await s.commit()
        roles = tuple(r for r in (row[4] or "").split(",") if r)
        user = User(id=row[1], upn=row[2], display_name=row[3], roles=roles)
        return await self._still_allowed(user, row[5])

    async def _user_for_oauth_token(self, token: str) -> User | None:
        async with self._sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT t.id, u.id, u.upn, u.display_name, u.roles, u.directory_checked_at"
                        " FROM itops.oauth_tokens t JOIN itops.users u ON u.id = t.user_id"
                        " WHERE t.token_hash = :h AND t.kind = 'access' AND t.revoked_at IS NULL"
                        " AND t.expires_at > :n"
                    ),
                    {"h": _hash(token), "n": _now()},
                )
            ).first()
            if row is None:
                return None
            await s.execute(
                text(
                    "UPDATE itops.oauth_tokens SET last_used_at = :n WHERE grant_id ="
                    " (SELECT grant_id FROM itops.oauth_tokens WHERE id = :i) AND kind = 'refresh'"
                    " AND (last_used_at IS NULL OR last_used_at < DATEADD(minute, -5, :n))"
                ),
                {"i": row[0], "n": _now()},
            )
            await s.commit()
        roles = tuple(r for r in (row[4] or "").split(",") if r)
        user = User(id=row[1], upn=row[2], display_name=row[3], roles=roles)
        return await self._still_allowed(user, row[5])

    async def still_allowed(self, user_id: str) -> bool:
        """A fresh AD check of a token owner (OAuth refreshes ask this every time)."""
        async with self._sessionmaker() as s:
            row = (
                await s.execute(
                    text("SELECT id, upn, display_name, roles FROM itops.users WHERE id = :u"),
                    {"u": user_id},
                )
            ).first()
        if row is None:
            return False
        roles = tuple(r for r in (row[3] or "").split(",") if r)
        user = User(id=row[0], upn=row[1], display_name=row[2], roles=roles)
        return await self._still_allowed(user, None, force=True) is not None

    async def _still_allowed(
        self, user: User, checked_at: dt.datetime | None, *, force: bool = False
    ) -> User | None:
        """The token owner, with roles fresh from AD; None (and every token of theirs
        revoked) if they're disabled, gone, or in no IT-Ops group. Checked at most hourly;
        if AD can't be reached, what was known stands, and AD isn't asked again for a few
        minutes. Concurrent requests for one person share a single check."""
        if self.directory is None:
            return user if user.roles else None
        fresh = checked_at is not None and checked_at > _now() - dt.timedelta(
            minutes=DIRECTORY_CHECK_MINUTES
        )
        failed_at = self._directory_failed.get(user.id)
        backing_off = failed_at is not None and (
            time.monotonic() - failed_at < DIRECTORY_RETRY_MINUTES * 60
        )
        if (fresh and not force) or backing_off:
            return user if user.roles else None
        running = self._directory_checks.get(user.id)
        if running is None:
            running = asyncio.ensure_future(self._refresh_from_directory(user))
            self._directory_checks[user.id] = running
            running.add_done_callback(lambda _: self._directory_checks.pop(user.id, None))
        return await asyncio.shield(running)

    async def _refresh_from_directory(self, user: User) -> User | None:
        assert self.directory is not None
        answer = await self.directory(user.upn, list(self.role_groups.values()))
        if answer.status == "error":
            self._directory_failed[user.id] = time.monotonic()
            return user if user.roles else None
        self._directory_failed.pop(user.id, None)
        roles = tuple(roles_from_groups(answer.groups, self.role_groups))
        now = _now()
        async with self._sessionmaker() as s:
            await s.execute(
                text("UPDATE itops.users SET roles = :r, directory_checked_at = :n WHERE id = :u"),
                {"r": ",".join(roles), "n": now, "u": user.id},
            )
            if not roles:
                await s.execute(
                    text(
                        "UPDATE itops.api_tokens SET revoked_at = :n"
                        " WHERE user_id = :u AND revoked_at IS NULL"
                    ),
                    {"n": now, "u": user.id},
                )
                await s.execute(
                    text(
                        "UPDATE itops.oauth_tokens SET revoked_at = :n"
                        " WHERE user_id = :u AND revoked_at IS NULL"
                    ),
                    {"n": now, "u": user.id},
                )
                why = {"active": "is in no IT-Ops group", "disabled": "is disabled",
                       "missing": "is no longer in AD"}[answer.status]  # fmt: skip
                await self._record(
                    s, user, "user.access_removed",
                    f"Revoked their tokens and AI connections: their account {why}",
                    directory=answer.status,
                )  # fmt: skip
            elif set(roles) != set(user.roles):
                await self._record(
                    s, user, "user.roles_changed", f"Roles now {top_role(roles)} (from AD)",
                    roles=list(roles), before=list(user.roles),
                )  # fmt: skip
            await s.commit()
        if not roles:
            return None
        return User(id=user.id, upn=user.upn, display_name=user.display_name, roles=roles)

    async def token_known(self, token: str) -> bool:
        """Was this ever a real token (even if expired or revoked)? The rate limiter holds
        off addresses presenting made-up tokens, not ones with stale tokens."""
        if not token.startswith(TOKEN_PREFIX):
            return False
        async with self._sessionmaker() as s:
            found = (
                await s.execute(
                    text(
                        "SELECT CASE WHEN EXISTS (SELECT 1 FROM itops.api_tokens"
                        " WHERE token_hash = :h) OR EXISTS (SELECT 1 FROM itops.oauth_tokens"
                        " WHERE token_hash = :h) THEN 1 ELSE 0 END"
                    ),
                    {"h": _hash(token)},
                )
            ).scalar()
        return bool(found)


class PatVerifier:
    """MCP SDK token verifier: personal access tokens and OAuth access tokens (ADR 0013) ->
    the MCP request's identity."""

    def __init__(self, identity: Identity) -> None:
        self._identity = identity

    async def verify_token(self, token: str) -> AccessToken | None:
        user = await self._identity.user_for_token(token)
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id="itops-pat",
            scopes=list(user.roles),
            subject=user.id,
            claims={"upn": user.upn, "name": user.display_name},
        )


def principal_from_access_token(token: Any, identity: Identity) -> Principal:
    """Build the via-AI principal for an MCP call from the SDK's verified token."""
    claims: dict[str, Any] = getattr(token, "claims", None) or {}
    user = User(
        id=str(getattr(token, "subject", "") or ""),
        upn=str(claims.get("upn", "")),
        display_name=str(claims.get("name", "")),
        roles=tuple(getattr(token, "scopes", ()) or ()),
    )
    return identity.principal(user, via_ai=True, client="mcp")
