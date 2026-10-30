"""One record per machine across sources (ADR 0017): identities and matching in upsert."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
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
        assert dry == {"outcome": "matched", "id": by_serial, "url": f"/assets/r/{by_serial}"}

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
        assert "added as a new device: probably a replacement" in new["problem"]
        assert new["problem_url"].startswith("/assets/r/")

        await person_creates("HOST06", serial_number="DUP")
        await person_creates("HOST07", serial_number="dup")
        unsure = await sync("6", "HOST06", {}, serial="DUP")
        assert unsure["outcome"] == "failed" and "matches 2 records" in unsure["problem"]
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


async def test_the_fleet_gives_each_device_an_age_from_purchase_or_warranty(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path):
        await person_creates("HOST01", kind="Laptop", purchased="2021-03-01",
                             warranty_expires="2024-03-01")  # fmt: skip
        await person_creates("HOST02", kind="Workstation", warranty_expires="2025-06-30")
        await person_creates("HOST03", kind="Workstation", status="Ignored")
        await person_creates("HOST04", kind="Printer")
        await person_creates("HOST05", kind="Laptop", manufacturer="HP", serial_number="MXL0042ABC")
        await person_creates("HOST06", kind="Laptop", manufacturer="HP", serial_number="MXL0042ABC",
                             in_service="2020-06-01")  # fmt: skip
        await person_creates("HOST07", kind="Laptop", ad_joined="2024-04-10")
        with acting_as(PERSON):
            found = {d["name"]: d for d in await service.fleet()}
        assert sorted(found) == [
            "HOST01",
            "HOST02",
            "HOST04",
            "HOST05",
            "HOST06",
            "HOST07",
        ]  # Ignored is left out
        assert (found["HOST01"]["acquired"], found["HOST01"]["age_basis"]) == (
            "2021-03-01", "purchased")  # fmt: skip
        assert (found["HOST02"]["acquired"], found["HOST02"]["age_basis"]) == (
            "2022-06-30", "warranty")  # fmt: skip
        assert found["HOST04"]["acquired"] is None and found["HOST04"]["kind"] == "Printer"
        # No dates, but HP's serial carries the build week: an estimate, and it says so.
        assert (found["HOST05"]["acquired"], found["HOST05"]["age_basis"]) == (
            "2020-01-20", "serial")  # fmt: skip
        # A person's estimate beats the serial's.
        assert (found["HOST06"]["acquired"], found["HOST06"]["age_basis"]) == (
            "2020-06-01", "in_service")  # fmt: skip
        # Last: when it joined the domain.
        assert (found["HOST07"]["acquired"], found["HOST07"]["age_basis"]) == (
            "2024-04-10", "joined")  # fmt: skip
        assert found["HOST01"]["url"].startswith("/assets/r/")


async def test_a_sparse_field_keeps_another_sources_value(
    database_url: str, tmp_path: Path
) -> None:
    owns = {"assets.device": ["model", "warranty_expires"]}

    async def sync_dates(fields: dict[str, Any], sparse: tuple[str, ...]) -> dict[str, Any]:
        with acting_as(CONNECTOR), connector_run("fake.inventory", owns=owns):
            return await service.upsert(source_type="machine", source_id="1", type_key="device",
                                        name="HOST01", fields=fields, sparse=sparse)  # fmt: skip

    async with running(database_url, tmp_path):
        first = await sync_dates({"model": "M1", "warranty_expires": "2028-02-29"}, ())
        # A later sync without the date, sparse: the date stays.
        await sync_dates({"model": "M2"}, ("warranty_expires",))
        async with module.session() as s:
            record = await service.get(s, first["id"])
            assert record is not None
            assert service.record_fields(record)["warranty_expires"] == "2028-02-29"
        # Not sparse: a blank from the owning source clears what it sent.
        await sync_dates({"model": "M3", "warranty_expires": "2029-01-31"}, ())
        await sync_dates({"model": "M4"}, ())
        async with module.session() as s:
            record = await service.get(s, first["id"])
            assert record is not None and "warranty_expires" not in service.record_fields(record)


async def test_people_fill_an_owned_field_the_source_left_empty(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owns = {"assets.device": ["model", "purchased"]}

    async def owned(record: Any) -> frozenset[str]:  # the test connector isn't registered
        return frozenset(owns["assets.device"])

    monkeypatch.setattr(service, "owned", owned)

    async def sync_dates(fields: dict[str, Any]) -> dict[str, Any]:
        with acting_as(CONNECTOR), connector_run("fake.inventory", owns=owns):
            return await service.upsert(source_type="machine", source_id="1", type_key="device",
                                        name="HOST01", fields=fields,
                                        sparse=("purchased",))  # fmt: skip

    async def person_sets(record_id: str, **fields: Any) -> dict[str, Any]:
        async with module.session() as s:
            record = await service.get(s, record_id)
            assert record is not None
            with acting_as(PERSON):
                values = {**service.record_fields(record), **fields}
                await service.update(s, PERSON, record, name=record.name, fields=values)
            await s.commit()
            return service.record_fields(record)

    async with running(database_url, tmp_path):
        rid = (await sync_dates({"model": "M1"}))["id"]
        # The source has no purchase date: a person may add one; the model stays the source's.
        values = await person_sets(rid, purchased="2021-07-27", model="Mine")
        assert values["purchased"] == "2021-07-27" and values["model"] == "M1"
        # A later sync without a date keeps it; once the source has one, it wins.
        await sync_dates({"model": "M1"})
        await sync_dates({"model": "M1", "purchased": "2021-06-30"})
        values = await person_sets(rid, purchased="2020-01-01")
        assert values["purchased"] == "2021-06-30"
        # Someone clears the (wrong) date in the source: it's kept, but no longer the
        # source's, so a person can correct it or clear it.
        await sync_dates({"model": "M2"})
        values = await person_sets(rid, purchased="2025-04-01")
        assert values["purchased"] == "2025-04-01"
        values = await person_sets(rid, purchased=None)
        assert not values.get("purchased")


async def test_a_date_the_owning_source_never_sends_can_be_corrected(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another import wrote a (wrong) purchase date; the source that owns the field never
    sends one (Lansweeper and HP computers). Even an unchanged sync lets people fix it."""
    owns = {"assets.device": ["model", "purchased"]}

    async def owned(record: Any) -> frozenset[str]:  # the test connector isn't registered
        return frozenset(owns["assets.device"])

    monkeypatch.setattr(service, "owned", owned)

    async def sync() -> dict[str, Any]:
        with acting_as(CONNECTOR), connector_run("fake.inventory", owns=owns):
            return await service.upsert(source_type="machine", source_id="1", type_key="device",
                                        name="HOST01", fields={"model": "M1"},
                                        sparse=("purchased",))  # fmt: skip

    async with running(database_url, tmp_path):
        rid = (await sync())["id"]
        async with module.session() as s:
            record = await service.get(s, rid)
            assert record is not None
            with acting_as(CONNECTOR), connector_run("fake.other", owns={}):
                values = {**service.record_fields(record), "purchased": "2021-01-22"}
                await service.update(s, CONNECTOR, record, name=record.name, fields=values)
            await s.commit()
        assert (await sync())["outcome"] == "unchanged"
        async with module.session() as s:
            record = await service.get(s, rid)
            assert record is not None
            with acting_as(PERSON):
                values = {**service.record_fields(record), "purchased": None}
                await service.update(s, PERSON, record, name=record.name, fields=values)
            await s.commit()
            assert not service.record_fields(record).get("purchased")


async def test_a_value_a_person_filled_survives_syncs_until_the_source_has_one(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not sparse: the source's blank clears what it sent itself, never what a person filled
    while it had nothing."""

    async def owned(record: Any) -> frozenset[str]:  # the test connector isn't registered
        return frozenset(OWNS["assets.device"])

    monkeypatch.setattr(service, "owned", owned)

    async def values_of(record_id: str) -> dict[str, Any]:
        async with module.session() as s:
            record = await service.get(s, record_id)
            assert record is not None
            return service.record_fields(record)

    async with running(database_url, tmp_path):
        rid = (await sync("1", "HOST01", {"model": "M1"}))["id"]
        async with module.session() as s:
            record = await service.get(s, rid)
            assert record is not None
            with acting_as(PERSON):
                values = service.record_fields(record) | {"serial_number": "SN-9"}
                await service.update(s, PERSON, record, name=record.name, fields=values)
            await s.commit()
            assert await service.filled_fields(s, record.id) == {"serial_number"}
        # The source record changed (a new model), still without a serial: the person's stays.
        assert (await sync("1", "HOST01", {"model": "M2"}))["outcome"] == "updated"
        assert (await values_of(rid))["serial_number"] == "SN-9"
        # A person can still correct it while the source has none.
        async with module.session() as s:
            record = await service.get(s, rid)
            assert record is not None
            with acting_as(PERSON):
                values = service.record_fields(record) | {"serial_number": "SN-10"}
                assert await service.update(s, PERSON, record, name=record.name, fields=values)
            await s.commit()
        # The source sends one: it wins, and from then on it's the source's to clear.
        await sync("1", "HOST01", {"model": "M2", "serial_number": "SN-1"})
        assert (await values_of(rid))["serial_number"] == "SN-1"
        async with module.session() as s:
            assert await service.filled_fields(s, uuid.UUID(rid)) == frozenset()
        await sync("1", "HOST01", {"model": "M3"})
        assert "serial_number" not in await values_of(rid)


async def test_a_value_a_merge_filled_survives_syncs(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def owned(record: Any) -> frozenset[str]:
        return frozenset(OWNS["assets.device"])

    monkeypatch.setattr(service, "owned", owned)
    async with running(database_url, tmp_path):
        live = (await sync("1", "HOST01", {"serial_number": "SN-1"}))["id"]
        old = await person_creates("HOST01-OLD", model="M7", purchased="2022-09-01")
        async with module.session() as s:
            with acting_as(PERSON):
                stale, kept = await service.get(s, old), await service.get(s, live)
                assert stale is not None and kept is not None
                assert await service.merge(s, PERSON, stale, kept) == ["model", "purchased"]
            await s.commit()
        await sync("1", "HOST01", {"serial_number": "SN-1", "mac_address": "AA:BB:CC:DD:EE:01"})
        async with module.session() as s:
            record = await service.get(s, live)
            assert record is not None and service.record_fields(record)["model"] == "M7"


async def test_ad_matches_by_host_name_and_creates_the_rest(
    database_url: str, tmp_path: Path
) -> None:
    owns = {"assets.device": ["ad_last_sign_in", "ad_account"]}

    async def sync_ad(name: str, **fields: Any) -> dict[str, Any]:
        with acting_as(CONNECTOR), connector_run("fake.directory", owns=owns):
            return await service.upsert(source_type="computer", source_id=name,
                                        type_key="device", name=name, fields=fields,
                                        identity={"hostname": name})  # fmt: skip

    today = dt.date.today().isoformat()
    async with running(database_url, tmp_path):
        rid = await person_creates("HOST01", kind="Laptop", hostname="host01.example.com")
        matched = await sync_ad("HOST01", ad_last_sign_in=today, ad_account="Enabled")
        assert (matched["outcome"], matched["id"]) == ("matched", rid)
        # An old account nothing matches: a Retired device, still enabled in AD.
        await sync_ad("HOST99", kind="Laptop", status="Retired", ad_last_sign_in="2024-01-01",
                      ad_account="Enabled")  # fmt: skip
        with acting_as(PERSON):
            found = {d["name"]: d for d in await service.fleet()}
            # AD saw HOST01, no scan did: in use on AD's word, and listed.
            assert found["HOST01"]["seen_by"] == "ad"
            assert found["HOST99"]["seen_by"] == ""
            listed = {d["name"]: d["disagreements"] for d in await service.disagreements()}
        assert sorted(listed) == ["HOST01", "HOST99"]
        [why] = listed["HOST99"]
        assert why.startswith("Marked Retired, but its AD account is still enabled: disable it")


async def test_a_source_that_recreates_a_machine_under_a_new_id_keeps_one_record(
    database_url: str, tmp_path: Path
) -> None:
    # Lansweeper gave one PC three ids in five days (97730, 97733, 97751): each new id
    # made another record. A new id with the same serial takes over when the old one wasn't
    # seen in this run; the old reference is released, so nothing is flagged missing.
    async def sync_run(run_id: int, source_id: str, serial: str) -> dict[str, Any]:
        with acting_as(CONNECTOR), connector_run("fake.inventory", owns=OWNS, run_id=run_id):
            return await service.upsert(source_type="machine", source_id=source_id,
                                        type_key="device", name="HOST01",
                                        fields={"model": "M1", "serial_number": serial},
                                        identity={"serial": serial})  # fmt: skip

    async with running(database_url, tmp_path):
        first = await sync_run(1, "97730", "SN-1")
        assert first["outcome"] == "created"
        second = await sync_run(2, "97733", "SN-1")
        assert (second["outcome"], second["id"]) == ("matched", first["id"])
        links = await module.refs.links("device", first["id"])
        assert [x.source_id for x in links] == ["97733"]  # the old id is gone
        # Two live ids in the same run (a real duplicate in the source): no takeover.
        third = await sync_run(3, "97733", "SN-1")
        dup = await sync_run(3, "99999", "SN-1")
        assert third["id"] == first["id"] and dup["outcome"] == "created"
        # Another serial is another machine.
        other = await sync_run(4, "50000", "SN-2")
        assert other["id"] != first["id"]
