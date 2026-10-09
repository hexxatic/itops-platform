"""Assets service (ADR 0009): starter types, direct edits, indexing, lookup, AI proposals."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from itops_assets import module, service
from itops_assets.service import AssetsError
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, make_app

PERSON = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
AI = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}),
               via_ai=True, client="mcp")  # fmt: skip


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path, *, fresh_types: bool = False
) -> AsyncGenerator[None]:
    if fresh_types:  # start from no asset types, so the next start seeds the starter types
        app = make_app(database_url, [module], files_dir=tmp_path)
        async with app.router.lifespan_context(app), module.session() as s:
            await s.execute(text("DELETE FROM itops.record_types WHERE module = 'assets'"))
            await s.commit()
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM assets.proposals"))
            await s.execute(text("DELETE FROM assets.records"))
            await s.commit()
        yield


async def test_starter_types_are_seeded_once_and_never_overwritten(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path, fresh_types=True):
        keys = {t.key for t in await service.available_types()}
        assert {"device", "ssl_certificate", "contact", "location", "domain", "ip_address"} <= keys
        # An admin changes a starter type...
        runtime = module.runtime
        with acting_as(PERSON):
            async with module.session() as s:
                device = await runtime.types.get(s, "assets", "device")
                assert device and device.published
                changed = device.published.definition.model_dump()
                changed["fields"].append({"key": "rack", "label": "Rack", "kind": "text"})
                await runtime.types.draft(s, "assets", changed, key="device")
                await runtime.types.publish(s, "assets", "device")
                await s.commit()
    # ...and a restart keeps the change.
    async with running(database_url, tmp_path):
        assert "rack" in {f.key for f in (await service.type_definition("device")).fields}


async def test_people_edit_directly_and_changes_are_audited(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path):
        async with module.session() as s:
            plant = await service.create(s, PERSON, type_key="location", name="North plant",
                                        fields={"city": "Northville"})  # fmt: skip
            sql = await service.create(s, PERSON, type_key="device", name="DB-SRV01", fields={
                "kind": "server", "primary_ip": "192.0.2.15", "warranty_expires": "2029-07-31",
                "location": f"assets:location:{plant.id}"})  # fmt: skip
            await s.commit()
            assert service.record_fields(sql)["kind"] == "Server"  # normalized to the option

            with pytest.raises(AssetsError, match="Valid until is required"):
                await service.create(
                    s, PERSON, type_key="ssl_certificate", name="itops.example.com"
                )
            with pytest.raises(AssetsError, match="isn't an available asset type"):
                await service.create(s, PERSON, type_key="spaceship", name="x")

            changed = await service.update(s, PERSON, sql, name="DB-SRV01", fields={
                **service.record_fields(sql), "primary_ip": "192.0.2.16"})  # fmt: skip
            await s.commit()
            assert changed == ["primary_ip"]
            same = service.record_fields(sql)
            assert await service.update(s, PERSON, sql, name="DB-SRV01", fields=same) == []

            # Queryable values and search (links and credentials stay out of search text).
            sql_fields = text(
                "SELECT field_key, value_text, value_date FROM assets.record_fields"
                " WHERE record_id = :r"
            )
            rows = await s.execute(sql_fields, {"r": sql.id})
            values = {r[0]: (r[1], r[2]) for r in rows.all()}
            assert values["primary_ip"][0] == "192.0.2.16"
            assert str(values["warranty_expires"][1]) == "2029-07-31"
            sql_search = text(
                "SELECT text FROM itops.search_passages WHERE module = 'assets' AND entity_id = :r"
            )
            passage = (await s.execute(sql_search, {"r": str(sql.id)})).scalar()
            assert passage and "DB-SRV01" in passage and "192.0.2.16" in passage

            sql_audit = text(
                "SELECT action, summary FROM itops.audit_log WHERE module = 'assets'"
                " AND entity_id = :r ORDER BY id"
            )
            audit = (await s.execute(sql_audit, {"r": str(sql.id)})).all()
            assert [a[0] for a in audit] == ["asset.created", "asset.updated"]
            assert audit[1][1] == "Changed primary_ip"

            found = await service.records(s, type_key="device", query="2.16")
            assert [r.name for r in found] == ["DB-SRV01"]

        # Other modules link to assets through the lookup service.
        by_kind = await service.lookup(kind="location")
        assert by_kind == [{"id": str(plant.id), "kind": "location", "name": "North plant",
                            "url": f"/assets/r/{plant.id}"}]  # fmt: skip
        assert (await service.lookup(ids=[str(sql.id), "not-an-id"]))[0]["name"] == "DB-SRV01"

        async with module.session() as s:
            rec = await service.get(s, sql.id)
            assert rec is not None
            with pytest.raises(AssetsError, match="done by a person"):
                await service.delete_record(s, AI, rec)
            await service.delete_record(s, PERSON, rec)
            await s.commit()
            assert await service.get(s, sql.id) is None


async def test_ai_proposes_a_person_approves(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path), module.session() as s:
        expiry = {"expires": "2027-01-15"}
        p = await service.propose(
            s, AI, type_key="domain", name="example.com", fields=expiry, note="from WHOIS"
        )
        await s.commit()
        assert p.proposed_via_ai and p.status == "pending"
        assert await service.records(s, type_key="domain") == []  # nothing applied yet
        with pytest.raises(AssetsError, match="done by a person"):
            await service.decide(s, AI, p, approve=True)

        record = await service.decide(s, PERSON, p, approve=True)
        await s.commit()
        assert record is not None and record.created_by == PERSON.user
        assert service.record_fields(record) == expiry

        change = await service.propose(
            s, AI, record=record, name="example.com", fields={"expires": "2028-01-15"}
        )
        await service.decide(s, PERSON, change, approve=False)
        await s.commit()
        assert service.record_fields(record) == expiry  # rejected: unchanged
        assert await service.pending_proposals(s) == []
        with pytest.raises(AssetsError, match="already decided"):
            await service.decide(s, PERSON, change, approve=True)


async def test_ai_cannot_write_assets_directly(database_url: str, tmp_path: Path) -> None:
    """A token holder with assets.write is still an AI connection: over REST it can't create
    or change a record; it proposes, and a person approves."""
    async with running(database_url, tmp_path), module.session() as s:
        with acting_as(AI), pytest.raises(AssetsError, match="proposes"):
            await service.create(s, AI, type_key="domain", name="ai.example.com")
        with acting_as(PERSON):
            record = await service.create(s, PERSON, type_key="domain", name="mine.example.com")
            await s.commit()
        with acting_as(AI), pytest.raises(AssetsError, match="proposes"):
            await service.update(s, AI, record, name="changed.example.com", fields={})
