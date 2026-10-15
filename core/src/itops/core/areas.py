"""Visibility of areas by AD group (ADR 0012).

An **area** is a part of a module people can be kept out of: a Docs collection, an asset
type. A module declares its area kinds in the manifest (``[areas] collection =
"Collection"``); an admin restricts an area to AD groups, keeps it out of AI connections,
or both. Restricting only narrows: roles still say what someone may do, an area's groups
say where.

Who can't see a restricted area: everyone acting *via AI* when it's kept out of AI (admins
included); otherwise anyone who isn't an admin and is in none of its groups. Connector
runs act as themselves, not as people, and see everything; so does code running with no
principal at all (startup indexing).

Restrictions are few and read on every request, so they're cached per process and
reloaded every ``REFRESH_SECONDS`` (other worker processes' changes arrive within that) and
at once in the process that made the change.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core import auth
from itops.core.audit import AuditLog
from itops.core.registry import ServiceRegistry

AREAS_PERMISSION = "itops.areas.manage"  # admins (no role grants it), and human-only
REFRESH_SECONDS = 30.0
MAX_GROUPS = 20
GROUP_RE = re.compile(r"^[^\"/\\\[\]:;|=,+*?<>@]{1,64}$")  # a sAMAccountName

# A module's description of one of its areas: {"name": ..., "url": ...} or None (no such
# area). Modules with [areas] provide it as the ``describe_area`` service.
Describe = Callable[..., Awaitable[dict[str, str] | None]]
# Does an AD group exist? True/False, or None when AD can't tell (dev, an outage).
GroupCheck = Callable[[str], Awaitable[bool | None]]


class AreaError(ValueError):
    """A user-facing problem with a restriction (shown as a message)."""


def area_id(module: str, kind: str, key: str) -> str:
    return f"{module}:{kind}:{key}"


def clean_groups(raw: Iterable[str]) -> list[str]:
    """Group names as an admin typed them (one per line or comma-separated, ``DOMAIN\\``
    prefixes dropped), de-duplicated ignoring case."""
    out: list[str] = []
    for chunk in raw:
        for name in re.split(r"[,\n]", chunk):
            name = name.strip().split("\\")[-1].strip()
            if not name:
                continue
            if not GROUP_RE.match(name):
                raise AreaError(f"“{name}” isn't an AD group name (its sAMAccountName).")
            if name.lower() not in {g.lower() for g in out}:
                out.append(name)
    if len(out) > MAX_GROUPS:
        raise AreaError(f"At most {MAX_GROUPS} groups per area.")
    return out


@dataclass(frozen=True)
class Restriction:
    module: str
    kind: str
    key: str
    groups: tuple[str, ...]  # as an admin typed them; compared ignoring case
    no_ai: bool
    updated_by: str = ""
    updated_at: dt.datetime | None = None

    @property
    def id(self) -> str:
        return area_id(self.module, self.kind, self.key)

    def hides_from(self, p: auth.Principal | None) -> bool:
        if p is None or p.client == "connector":
            return False
        if self.no_ai and p.via_ai:
            return True
        if not self.groups or auth.ALL in p.permissions:
            return False
        return not ({g.lower() for g in self.groups} & p.groups)


class Areas:
    def __init__(self) -> None:
        self.sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self.audit: AuditLog = AuditLog()
        self.group_exists: GroupCheck | None = None  # set by the app where AD is reachable
        self.registry: ServiceRegistry | None = None  # modules' area_entities services
        self.on_change: list[Callable[[], Awaitable[None] | None]] = []
        self.kinds: dict[str, dict[str, str]] = {}  # module -> {kind: title}
        self._restrictions: dict[str, Restriction] = {}
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    def declare(self, module: str, kinds: dict[str, str]) -> None:
        if kinds:
            self.kinds[module] = dict(kinds)

    def check_kind(self, module: str, kind: str) -> None:
        if kind not in self.kinds.get(module, {}):
            raise AreaError(f"{module} has no area kind {kind!r} (declare it under [areas]).")

    # ---- reading -----------------------------------------------------------------------

    async def _current(self) -> dict[str, Restriction]:
        stale = self._loaded_at is None or time.monotonic() - self._loaded_at > REFRESH_SECONDS
        if stale and self.sessionmaker is not None:
            async with self._lock:
                if self._loaded_at is None or time.monotonic() - self._loaded_at > REFRESH_SECONDS:
                    await self.reload()
        return self._restrictions

    async def reload(self) -> None:
        assert self.sessionmaker is not None
        async with self.sessionmaker() as s:
            rows = await s.execute(
                text(
                    "SELECT module, kind, area_key, group_names, no_ai, updated_by, updated_at"
                    " FROM itops.area_restrictions"
                )
            )
            listed = [
                Restriction(
                    module=row[0],
                    kind=row[1],
                    key=row[2],
                    groups=tuple(g for g in (row[3] or "").split(",") if g),
                    no_ai=bool(row[4]),
                    updated_by=row[5],
                    updated_at=row[6],
                )
                for row in rows.all()
            ]
        found = {r.id: r for r in listed}
        self._restrictions, self._loaded_at = found, time.monotonic()

    async def restricted(self) -> frozenset[str]:
        """Every restricted area's id (the search vocabulary leaves them out)."""
        return frozenset(await self._current())

    async def hidden(
        self, principal: auth.Principal | None, module: str | None = None, kind: str | None = None
    ) -> frozenset[str]:
        """What ``principal`` can't see: area ids, or with ``module`` and ``kind`` that
        module's keys of that kind. Usually empty."""
        out: set[str] = set()
        for r in (await self._current()).values():
            mine = (module is None or r.module == module) and (kind is None or r.kind == kind)
            if mine and r.hides_from(principal):
                out.add(r.key if module is not None and kind is not None else r.id)
        return frozenset(out)

    async def get(self, module: str, kind: str, key: str) -> Restriction | None:
        return (await self._current()).get(area_id(module, kind, key))

    async def access(
        self, principal: auth.Principal | None, module: str, kind: str, key: str
    ) -> dict[str, object]:
        """What a page shows about an area (``ui.access_summary``): restricted and to
        which groups, kept out of AI, and the access page's url if ``principal`` may
        change it."""
        self.check_kind(module, kind)
        r = await self.get(module, kind, key)
        manage = bool(principal and not principal.via_ai and principal.has(AREAS_PERMISSION))
        return {
            "restricted": r is not None and bool(r.groups),
            "groups": list(r.groups) if r else [],
            "no_ai": bool(r and r.no_ai),
            "href": f"/access/{module}/{kind}/{quote(key, safe='')}" if manage else "",
        }

    async def watched_groups(self) -> list[str]:
        """Every group a restriction names (the AD check asks token owners about them)."""
        names: dict[str, str] = {}
        for r in (await self._current()).values():
            names.update({g.lower(): g for g in r.groups})
        return sorted(names.values(), key=str.lower)

    # ---- changing (admins, in the browser) ----------------------------------------------

    async def set(
        self,
        module: str,
        kind: str,
        key: str,
        *,
        name: str,
        groups: Iterable[str],
        no_ai: bool,
    ) -> Restriction | None:
        """Restrict an area to ``groups`` and/or keep it out of AI; neither opens it again.
        ``name`` is the area's name, for the audit trail. Returns the new restriction."""
        self.check_kind(module, kind)
        who = auth.current()
        if who is None or who.via_ai or not who.has(AREAS_PERMISSION):
            raise auth.PermissionDenied("Only an admin, in the browser, changes who sees an area.")
        names = clean_groups(groups)
        if self.group_exists is not None:
            for g in names:
                if await self.group_exists(g) is False:
                    raise AreaError(f"There's no AD group called “{g}”.")
        before = await self.get(module, kind, key)
        assert self.sessionmaker is not None
        aid = area_id(module, kind, key)
        async with self.sessionmaker() as s:
            await s.execute(
                text("DELETE FROM itops.area_restrictions"
                     " WHERE module = :m AND kind = :k AND area_key = :a"),
                {"m": module, "k": kind, "a": key},
            )  # fmt: skip
            if names or no_ai:
                await s.execute(
                    text(
                        "INSERT INTO itops.area_restrictions"
                        " (module, kind, area_key, group_names, no_ai, updated_by)"
                        " VALUES (:m, :k, :a, :g, :n, :u)"
                    ),
                    {"m": module, "k": kind, "a": key, "g": ",".join(names), "n": no_ai,
                     "u": who.user},
                )  # fmt: skip
            action, summary = _describe(before, names, no_ai)
            if action:
                await self.audit.record(
                    s, module="itops", action=action, entity_kind="area", entity_id=aid,
                    title=name, summary=summary,
                    details={
                        "before": {"groups": list(before.groups), "no_ai": before.no_ai}
                        if before else None,
                        "after": {"groups": names, "no_ai": no_ai} if names or no_ai else None,
                    },
                )  # fmt: skip
            # Token owners are checked against AD for the groups restrictions name: check
            # everyone again at their next request, so a newly named group counts at once.
            if names:
                await s.execute(text("UPDATE itops.users SET directory_checked_at = NULL"))
            await self._stamp_search(s, module, kind, key)
            await s.commit()
        await self.reload()
        for fn in self.on_change:
            result = fn()
            if result is not None:
                await result
        return await self.get(module, kind, key)

    async def _stamp_search(self, s: AsyncSession, module: str, kind: str, key: str) -> None:
        """Mark the area's search entries with it, in the same transaction as the
        restriction: entries indexed before (or without) an area would otherwise still
        show. The module's ``area_entities(kind, key)`` service says what's in the area:
        ``{"kind": <search kind>, "ids": [...]}``."""
        fn = self.registry.get(f"{module}.area_entities") if self.registry else None
        if fn is None:
            return
        found: dict[str, Any] = await fn(kind=kind, key=key)
        ids = [str(i) for i in found.get("ids", [])]
        aid = area_id(module, kind, key)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            names = {f"e{i}": v for i, v in enumerate(chunk)}
            await s.execute(
                text(
                    "UPDATE itops.search_passages SET area = :a WHERE module = :m AND kind = :k"
                    " AND entity_id IN (" + ", ".join(":" + n for n in names) + ")"
                ),
                {"a": aid, "m": module, "k": found["kind"], **names},
            )


def _describe(before: Restriction | None, groups: list[str], no_ai: bool) -> tuple[str, str]:
    """The audit action and summary for a change (empty when nothing changed)."""
    after_groups = {g.lower() for g in groups}
    if (
        before is not None
        and {g.lower() for g in before.groups} == after_groups
        and (before.no_ai == no_ai)
    ):
        return "", ""
    if not groups and not no_ai:
        return "area.opened", "Opened to everyone with access to the module"
    parts = [f"Visible only to {', '.join(groups)} (and admins)"] if groups else []
    if no_ai:
        parts.append("kept out of AI connections" if groups else "Kept out of AI connections")
    return ("area.restricted" if before is None else "area.changed"), "; ".join(parts)
