"""The core audit trail: append-only, actor from the request, admins only."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from itops.core import auth, migrations
from itops.core.audit import AuditLog
from itops.core.db import make_engine, make_sessionmaker

ALICE_AI = auth.Principal("alice@example.com", "Alice", frozenset({"*"}), via_ai=True,
                          client="mcp")  # fmt: skip


async def test_actor_comes_from_the_request_and_rows_are_append_only(database_url: str) -> None:
    engine = make_engine(database_url)
    await migrations.upgrade_core(engine)
    maker = make_sessionmaker(engine)
    log = AuditLog()
    try:
        auth.set_current(ALICE_AI)
        async with maker() as s:
            await log.record(s, module="t", action="thing.done", entity_kind="thing",
                             entity_id="42", title="The thing", details={"n": 1})  # fmt: skip
            await s.commit()
        auth.set_current(None)
        async with maker() as s:
            [e] = await log.history(s, module="t", entity_kind="thing", entity_id="42", limit=1)
            assert (e.actor_user, e.via_ai, e.client, e.details) == (
                "alice@example.com", True, "mcp", {"n": 1},
            )  # fmt: skip
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(text("DELETE FROM itops.audit_log WHERE id = :i"), {"i": e.id})
        async with maker() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(text("UPDATE itops.audit_log SET summary = 'x'"))
    finally:
        auth.set_current(None)
        await engine.dispose()


def test_only_admins_can_read_the_trail() -> None:
    from itops.core.identity import permissions_for

    declared = ["docs.read", "docs.write", "docs.publish", "itops.audit"]
    assert not auth.Principal("p", "P", permissions_for(["publisher"], declared)).has("itops.audit")
    assert auth.Principal("a", "A", permissions_for(["admin"], declared)).has("itops.audit")
