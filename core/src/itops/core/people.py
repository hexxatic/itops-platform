"""People who use IT-Ops, for modules that point at a person (a contract's owner, later a
ticket's assignee): ADR 0014.

Everyone who has signed in has a row in ``itops.users`` (name, roles and groups as of their
last sign-in or AD check). A module stores a person by UPN and shows their current name;
pickers can be limited to people holding a permission (e.g. only people who can see
contracts may own one). People who never signed in aren't listed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core.auth import ALL

PermissionsOf = Callable[[Iterable[str], Iterable[str]], frozenset[str]]


@dataclass(frozen=True)
class Person:
    upn: str
    name: str


@dataclass
class People:
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    # (roles, groups) -> permissions; set by the app from Identity.
    permissions_of: PermissionsOf | None = None

    async def list(self, *, holding: str | None = None) -> list[Person]:
        """Everyone with an IT-Ops role, by name; with ``holding``, only people whose roles
        and groups grant that permission."""
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as s:
            rows = await s.execute(
                text(
                    "SELECT upn, display_name, roles, group_names FROM itops.users"
                    " WHERE roles IS NOT NULL AND roles <> '' ORDER BY display_name"
                )
            )
            found = rows.all()
        out: list[Person] = []
        for upn, name, roles, groups in found:
            if holding and self.permissions_of is not None:
                held = self.permissions_of(
                    str(roles).split(","), [g for g in str(groups or "").split(",") if g]
                )
                if ALL not in held and holding not in held:
                    continue
            out.append(Person(upn=str(upn), name=str(name)))
        return out

    async def names(self, upns: Iterable[str]) -> dict[str, str]:
        """UPN -> current display name (case-insensitive); unknown UPNs are left out."""
        wanted = sorted({u.lower() for u in upns if u})
        if not wanted or self.sessionmaker is None:
            return {}
        params = {f"u{i}": u for i, u in enumerate(wanted)}
        async with self.sessionmaker() as s:
            rows = await s.execute(
                text(
                    "SELECT upn, display_name FROM itops.users WHERE LOWER(upn) IN ("
                    + ", ".join(f":{k}" for k in params)
                    + ")"
                ),
                params,
            )
            found = {str(u).lower(): str(n) for u, n in rows.all()}
        return {u: found[u.lower()] for u in upns if u and u.lower() in found}
