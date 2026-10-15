"""One record per machine across sources (ADR 0017): identities and matching in upsert."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from itops_assets import identity, module, service
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing.app import acting_as, connector_run, make_app

PERSON = Principal(user="pat@example.com", display_name="Pat", permissions=frozenset({"*"}))
CONNECTOR = Principal(user="connector:fake.inventory", display_name="Inventory",
                      permissions=frozenset({"*"}))  # fmt: skip
OWNS = {"assets.device": ["model", "serial_number", "mac_address"]}


@asynccontextmanager
async def running(database_url: str, tmp_path: Path) -> AsyncGenerator[None]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM assets.records"))
            await s.execute(text("DELETE FROM itops.source_refs WHERE module = 'assets'"))
            await s.commit()
        yield


async def person_creates(name: str, **fields: Any) -> str:
    async with module.session() as s:
        with acting_as(PERSON):
            record = await service.create(s, PERSON, type_key="device", name=name, fields=fields)
        await s.commit()
        return str(record.id)


async def sync(source_id: str, name: str, fields: dict[str, Any], *, dry_run: bool = False,
               connector: str = "fake.inventory", **ident: Any) -> dict[str, Any]:  # fmt: skip
    with acting_as(CONNECTOR), connector_run(connector, owns=OWNS, dry_run=dry_run):
        return await service.upsert(source_type="machine", source_id=source_id, type_key="device",
                                    name=name, fields=fields, identity=ident)  # fmt: skip


def test_values_are_normalised() -> None:
    assert identity.serial("  abc 123 ") == "ABC 123"
    assert identity.serial("Default string") is None and identity.serial("") is None
    assert identity.mac("aa-bb-cc-dd-ee-0f") == "AA:BB:CC:DD:EE:0F"
    assert identity.mac("00:00:00:00:00:00") is None and identity.mac("nonsense") is None
    assert identity.macs("aa:bb:cc:dd:ee:01, AABBCCDDEE01\naa:bb:cc:dd:ee:02") == [
        "AA:BB:CC:DD:EE:01",
        "AA:BB:CC:DD:EE:02",
    ]
    assert identity.host("host01.example.com") == "HOST01"


async def test_a_new_source_links_to_the_machine_already_there(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path):
        by_serial = await person_creates("HOST01", serial_number="sn-1")
        by_mac = await person_creates("HOST02", mac_address="aa:bb:cc:dd:ee:02")
        by_host = await person_creates("HOST03", hostname="host03.example.com")

        # A dry run says it would match, and writes nothing.
        dry = await sync("1", "host01", {"model": "M1"}, dry_run=True, serial="SN-1")
        assert dry == {"outcome": "matched", "id": by_serial}

        one = await sync("1", "host01", {"model": "M1"}, serial="SN-1", hostname="other")
        two = await sync("2", "x", {"model": "M2"}, macs=["AABBCCDDEE02"])
        three = await sync("3", "HOST03", {"model": "M3"}, hostname="HOST03")
        assert [r["outcome"] for r in (one, two, three)] == ["matched"] * 3
        assert [r["id"] for r in (one, two, three)] == [by_serial, by_mac, by_host]
        async with module.session() as s:
            record = await service.get(s, by_serial)
            assert record is not None and record.name == "HOST01"  # the name isn't owned
            assert service.record_fields(record)["model"] == "M1"
        # From now on the source reference decides: no matching, normal updates.
        again = await sync("1", "host01", {"model": "M1b"}, serial="SN-1")
        assert again["outcome"] == "updated" and again["id"] == by_serial


async def test_replacements_and_ambiguity_are_not_guessed(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path):
        old = await person_creates("HOST05", serial_number="OLD-1")
        # The same host name with another serial: a replacement machine, a new record.
        new = await sync("5", "HOST05", {"serial_number": "NEW-1"}, serial="NEW-1",
                         hostname="HOST05")  # fmt: skip
        assert new["outcome"] == "created" and new["id"] != old
        assert "probably a replacement" in new["problem"]

        await person_creates("HOST06", serial_number="DUP")
        await person_creates("HOST07", serial_number="dup")
        unsure = await sync("6", "HOST06", {}, serial="DUP")
        assert unsure["outcome"] == "failed" and "several records" in unsure["problem"]
        async with module.session() as s:
            assert len(await service.records(s, type_key="device", query="HOST06")) == 1

        # A record this connector already syncs isn't matched by another of its items.
        first = await sync("8", "HOST08", {"serial_number": "S8"}, serial="S8")
        twin = await sync("9", "HOST08", {"serial_number": "S8"}, serial="S8")
        assert first["outcome"] == "created" and twin["outcome"] == "created"
        assert twin["id"] != first["id"]
        # ...but another connector's item is.
        other = await sync("1", "HOST08", {}, connector="fake.other", serial="S8")
        assert other["outcome"] == "failed"  # both HOST08 records have serial S8


async def test_identities_follow_the_values(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path):
        rid = await person_creates("HOST10", serial_number="S10", mac_address="aa:bb:cc:00:00:10")
        async with module.session() as s:
            rows = (await s.execute(text(
                "SELECT kind, value FROM assets.identities WHERE record_id = :r ORDER BY kind"
            ), {"r": rid})).all()  # fmt: skip
        assert [tuple(r) for r in rows] == [
            ("host", "HOST10"),
            ("mac", "AA:BB:CC:00:00:10"),
            ("serial", "S10"),
        ]


def test_ip_addresses_and_descriptions_are_not_host_names() -> None:
    assert identity.host("192.0.2.10") is None
    assert identity.host("Poly - Configuration Utility") is None
    assert identity.host("HOST01") == "HOST01"
