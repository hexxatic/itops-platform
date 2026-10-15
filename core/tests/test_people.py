"""People (ADR 0014): who can be picked as an owner, and their current names."""

from __future__ import annotations

from sqlalchemy import text

from itops.core import migrations
from itops.core.db import make_engine, make_sessionmaker
from itops.core.identity import Identity, User
from itops.core.people import People


async def test_people_are_listed_by_name_and_filtered_by_permission(database_url: str) -> None:
    engine = make_engine(database_url)
    await migrations.upgrade_core(engine)
    maker = make_sessionmaker(engine)
    async with maker() as s:
        await s.execute(text("DELETE FROM itops.users WHERE upn LIKE '%@people.example.com'"))
        await s.commit()
    identity = Identity(maker)
    identity.permission_groups = {"tcontracts.read": ["App-ITOps-Contracts"]}
    identity.set_declared_permissions(
        ["docs.read", "tcontracts.read"], group_granted=["tcontracts.read"]
    )
    for uid, name, roles, groups in [
        ("p1", "Paul Owner", ("reader",), ("app-itops-contracts",)),
        ("p2", "Ann Reader", ("reader",), ()),
        ("p3", "Zed Gone", (), ()),  # no role any more: not a person IT-Ops lists
    ]:
        await identity.record_sign_in(User(uid, f"{uid}@people.example.com", name, roles, groups))
    people = People(maker, identity.permissions_of)
    try:
        listed = [p.name for p in await people.list() if p.upn.endswith("@people.example.com")]
        assert listed == ["Ann Reader", "Paul Owner"]
        owners = await people.list(holding="tcontracts.read")
        assert [p.name for p in owners if p.upn.endswith("@people.example.com")] == ["Paul Owner"]
        names = await people.names(["P1@people.example.com", "nobody@people.example.com"])
        assert names == {"P1@people.example.com": "Paul Owner"}
    finally:
        await engine.dispose()
