"""The audit trail (ADR 0006): who did what, in every module, append-only.

The actor always comes from the current principal, never from the caller, so an entry
can't be misattributed and "via AI" is always right. Entries are written in the
caller's session: they commit or roll back with the change they describe.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from itops.core import auth

# Only admins hold it: roles grant permissions by their last word (read/write/publish).
AUDIT_PERMISSION = "itops.audit"


@dataclass(frozen=True)
class Entry:
    id: int
    at: dt.datetime
    module: str
    action: str
    entity_kind: str | None
    entity_id: str | None
    entity_title: str | None
    actor_user: str
    actor_name: str
    via_ai: bool
    client: str
    summary: str
    details: dict[str, Any] | None

    @property
    def verb(self) -> str:
        """``document.published`` -> ``published``."""
        return self.action.rsplit(".", 1)[-1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat() + "Z",
            "module": self.module,
            "action": self.action,
            "entity": {"kind": self.entity_kind, "id": self.entity_id, "title": self.entity_title},
            "by": self.actor_user,
            "via_ai": self.via_ai,
            "client": self.client,
            "summary": self.summary,
            "details": self.details,
        }


def _clip(value: str | None, n: int) -> str | None:
    return None if value is None else value[:n]


_COLUMNS = (
    "id, at, module, action, entity_kind, entity_id, entity_title, actor_user, actor_name,"
    " via_ai, client, summary, details"
)


def _entry(row: Any) -> Entry:
    return Entry(
        id=row[0],
        at=row[1],
        module=row[2],
        action=row[3],
        entity_kind=row[4],
        entity_id=row[5],
        entity_title=row[6],
        actor_user=row[7],
        actor_name=row[8],
        via_ai=bool(row[9]),
        client=row[10],
        summary=row[11],
        details=json.loads(row[12]) if row[12] else None,
    )


class AuditLog:
    async def record(
        self,
        session: AsyncSession,
        *,
        module: str,
        action: str,
        entity_kind: str | None = None,
        entity_id: str | None = None,
        title: str | None = None,
        summary: str = "",
        details: dict[str, Any] | None = None,
        actor: auth.Principal | None = None,
    ) -> None:
        """Add an entry in ``session`` (the caller commits). ``actor`` is for the core
        only (e.g. sign-in, before the principal exists); modules never pass it."""
        who = actor or auth.current()
        await session.execute(
            text(
                "INSERT INTO itops.audit_log (module, action, entity_kind, entity_id,"
                " entity_title, actor_user, actor_name, via_ai, client, summary, details)"
                " VALUES (:m, :a, :k, :i, :t, :u, :n, :ai, :c, :s, :d)"
            ),
            {
                "m": module,
                "a": _clip(action, 100),
                "k": _clip(entity_kind, 50),
                "i": _clip(entity_id, 100),
                "t": _clip(title, 300),
                "u": who.user if who else "system",
                "n": who.display_name if who else "System",
                "ai": bool(who and who.via_ai),
                "c": who.client if who else "system",
                "s": _clip(summary or action, 400),
                "d": json.dumps(details, default=str) if details else None,
            },
        )

    async def history(
        self,
        session: AsyncSession,
        *,
        module: str,
        entity_kind: str,
        entity_id: str,
        limit: int = 50,
    ) -> list[Entry]:
        """One entity's entries, newest first."""
        rows = await session.execute(
            text(
                f"SELECT TOP (:n) {_COLUMNS} FROM itops.audit_log"
                " WHERE module = :m AND entity_kind = :k AND entity_id = :i ORDER BY id DESC"
            ),
            {"n": limit, "m": module, "k": entity_kind, "i": entity_id},
        )
        return [_entry(r) for r in rows.all()]

    async def query(
        self,
        session: AsyncSession,
        *,
        module: str | None = None,
        actor: str | None = None,
        via_ai: bool = False,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[Entry]:
        """Everything, newest first; page with ``before_id`` (the last id shown)."""
        where = ["1 = 1"]
        params: dict[str, Any] = {"n": max(1, min(limit, 500))}
        if module:
            where.append("module = :m")
            params["m"] = module
        if actor:
            where.append("(actor_user LIKE :u OR actor_name LIKE :u)")
            params["u"] = f"%{actor}%"
        if via_ai:
            where.append("via_ai = 1")
        if before_id:
            where.append("id < :b")
            params["b"] = before_id
        rows = await session.execute(
            text(
                f"SELECT TOP (:n) {_COLUMNS} FROM itops.audit_log"
                f" WHERE {' AND '.join(where)} ORDER BY id DESC"
            ),
            params,
        )
        return [_entry(r) for r in rows.all()]

    async def modules(self, session: AsyncSession) -> list[str]:
        rows = await session.execute(text("SELECT DISTINCT module FROM itops.audit_log"))
        return sorted(r[0] for r in rows.all())
