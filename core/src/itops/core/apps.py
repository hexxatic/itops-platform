"""App tokens: another internal application reading IT-Ops as itself (ADR 0021).

A personal access token is a person *via AI*. A sister app (an end-user help site, a
dashboard) is neither: it calls the REST API as **itself**, with a fixed set of read
permissions an admin chose when issuing its token. So an app principal is:

* ``user = "app:<slug>"``, ``client = "app"``, never via AI, no AD groups;
* holding only ``*.read`` permissions of loaded modules, none of them human-only;
* kept out of every restricted area: one restricted to groups (it's in none), and one
  kept out of AI (an app may hand what it reads to its own assistant, ``areas.py``);
* on ``/api/`` and file downloads (``GET /files/...``, checked like anyone's) only. MCP is
  for AI tools acting as people; an app token is refused there, and on every web page.

Tokens are issued and revoked by admins in a browser (``/apps``), shown once, stored as a
SHA-256 hash, and expire (max a year). Issuing and revoking are audited.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core import auth
from itops.core.audit import AuditLog
from itops.core.auth import Principal, is_human_only
from itops.core.identity import hash_token

APP_TOKEN_PREFIX = "itops_app_"
MAX_APP_TOKEN_DAYS = 365
APP_CLIENT = "app"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$")


class AppTokenError(ValueError):
    """A user-facing problem issuing a token (shown as a message)."""


def readable(permissions: Iterable[str]) -> list[str]:
    """The permissions an app may be given: reads, never a human-only one."""
    return sorted(p for p in set(permissions) if p.endswith(".read") and not is_human_only(p))


@dataclass(frozen=True)
class AppToken:
    id: int
    slug: str
    name: str
    prefix: str
    permissions: tuple[str, ...]
    created_by: str
    created_at: dt.datetime
    expires_at: dt.datetime
    last_used_at: dt.datetime | None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


_COLUMNS = "id, slug, name, prefix, permissions, created_by, created_at, expires_at, last_used_at"


def _token(row: Any) -> AppToken:
    return AppToken(
        id=row[0], slug=row[1], name=row[2], prefix=row[3],
        permissions=tuple(p for p in (row[4] or "").split(",") if p),
        created_by=row[5], created_at=row[6], expires_at=row[7], last_used_at=row[8],
    )  # fmt: skip


class AppTokens:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        audit: AuditLog,
        declared: Callable[[], Iterable[str]],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._audit = audit
        self._declared = declared  # the loaded modules' permissions

    def choices(self) -> list[str]:
        return readable(self._declared())

    async def create(
        self, slug: str, name: str, permissions: Iterable[str], days: int = MAX_APP_TOKEN_DAYS
    ) -> str:
        """Returns the new token. It is never stored or shown again."""
        slug = slug.strip().lower()
        if not SLUG_RE.match(slug):
            raise AppTokenError(
                "The app's short name is 2 to 50 lower-case letters, digits and dashes."
            )
        name = " ".join(name.split())[:100] or slug
        allowed = set(self.choices())
        chosen = sorted(set(permissions))
        if not chosen:
            raise AppTokenError("Choose at least one permission.")
        if refused := [p for p in chosen if p not in allowed]:
            raise AppTokenError(f"An app can only be given read permissions, not {refused[0]}.")
        days = max(1, min(days, MAX_APP_TOKEN_DAYS))
        token = APP_TOKEN_PREFIX + secrets.token_urlsafe(32)
        prefix = token[: len(APP_TOKEN_PREFIX) + 6]
        async with self._sessionmaker() as s:
            await s.execute(
                text(
                    "INSERT INTO itops.app_tokens (slug, name, token_hash, prefix, permissions,"
                    " created_by, expires_at) VALUES (:s, :n, :h, :p, :perm, :by, :e)"
                ),
                {
                    "s": slug, "n": name, "h": hash_token(token), "p": prefix,
                    "perm": ",".join(chosen), "by": self._who(),
                    "e": _now() + dt.timedelta(days=days),
                },
            )  # fmt: skip
            await self._audit.record(
                s, module="itops", action="app_token.created", entity_kind="app",
                entity_id=slug, title=name,
                summary=f"Issued an app token for “{name}” ({days} days): {', '.join(chosen)}",
                details={"prefix": prefix, "days": days, "permissions": chosen},
            )  # fmt: skip
            await s.commit()
        return token

    async def live(self) -> list[AppToken]:
        async with self._sessionmaker() as s:
            rows = await s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM itops.app_tokens WHERE revoked_at IS NULL"
                    " AND expires_at > sysutcdatetime() ORDER BY slug, created_at DESC"
                )
            )
            return [_token(r) for r in rows.all()]

    async def revoke(self, token_id: int) -> bool:
        async with self._sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE itops.app_tokens SET revoked_at = sysutcdatetime()"
                        " OUTPUT inserted.slug, inserted.name, inserted.prefix"
                        " WHERE id = :i AND revoked_at IS NULL"
                    ),
                    {"i": token_id},
                )
            ).first()
            if row is None:
                return False
            await self._audit.record(
                s, module="itops", action="app_token.revoked", entity_kind="app",
                entity_id=row[0], title=row[1], summary=f"Revoked an app token for “{row[1]}”",
                details={"prefix": row[2]},
            )  # fmt: skip
            await s.commit()
            return True

    async def principal_for(self, token: str) -> Principal | None:
        """The app behind a live token, with the permissions it was given that the loaded
        modules still declare."""
        if not token.startswith(APP_TOKEN_PREFIX):
            return None
        async with self._sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM itops.app_tokens WHERE token_hash = :h"
                        " AND revoked_at IS NULL AND expires_at > sysutcdatetime()"
                    ),
                    {"h": hash_token(token)},
                )
            ).first()
            if row is None:
                return None
            await s.execute(
                text(
                    "UPDATE itops.app_tokens SET last_used_at = sysutcdatetime() WHERE id = :i"
                    " AND (last_used_at IS NULL"
                    "      OR last_used_at < DATEADD(minute, -5, sysutcdatetime()))"
                ),
                {"i": row[0]},
            )
            await s.commit()
        app = _token(row)
        allowed = set(self.choices())
        return Principal(
            user=f"app:{app.slug}",
            display_name=f"{app.name} (app)",
            permissions=frozenset(p for p in app.permissions if p in allowed),
            client=APP_CLIENT,
        )

    async def known(self, token: str) -> bool:
        """Was this ever a real app token (for the rate limiter, like ``token_known``)?"""
        if not token.startswith(APP_TOKEN_PREFIX):
            return False
        async with self._sessionmaker() as s:
            found = (
                await s.execute(
                    text("SELECT 1 FROM itops.app_tokens WHERE token_hash = :h"),
                    {"h": hash_token(token)},
                )
            ).scalar()
        return bool(found)

    @staticmethod
    def _who() -> str:
        p = auth.current()
        return p.user if p else "system"
