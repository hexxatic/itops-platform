"""Record types (ADR 0008): definitions, value validation, draft/publish, MCP tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from _helpers import MakeModule
from sqlalchemy import text

from itops.core import auth, migrations
from itops.core.db import make_engine, make_sessionmaker
from itops.core.manifest import Manifest
from itops.core.types import (
    TypeProblem,
    TypeStore,
    check_compatible,
    parse_definition,
    validate_values,
)

VENDOR: dict[str, Any] = {
    "name": "Vendor",
    "description": "Who we buy from and how to reach them",
    "fields": [
        {"key": "account_number", "label": "Account number", "kind": "text", "required": True},
        {"key": "support_phone", "label": "Support phone", "kind": "text"},
        {"key": "portal", "label": "Portal", "kind": "url"},
        {"key": "tier", "label": "Tier", "kind": "select", "options": ["Gold", "Silver"]},
        {"key": "renews", "label": "Renews", "kind": "date"},
        {"key": "seats", "label": "Seats", "kind": "number"},
        {"key": "login", "label": "Portal login", "kind": "credential"},
        {"key": "servers", "label": "Servers", "kind": "link", "target": "assets.device",
         "multiple": True},
    ],
}  # fmt: skip

ADMIN = auth.Principal("admin@example.com", "Admin", frozenset({"*"}))
ADMIN_AI = auth.Principal("admin@example.com", "Admin", frozenset({"*"}), via_ai=True,
                          client="mcp")  # fmt: skip
PUBLISHER = auth.Principal("pub@example.com", "Pub",
                           frozenset({"docs.read", "docs.write", "docs.publish"}))  # fmt: skip


# ---- definitions ---------------------------------------------------------------------------


def test_a_valid_definition_parses() -> None:
    d = parse_definition(VENDOR)
    assert [f.key for f in d.fields][:2] == ["account_number", "support_phone"]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ({"key": "tier", "label": "Tier", "kind": "select"}, "needs options"),
        ({"key": "srv", "label": "S", "kind": "link"}, "needs a target"),
        ({"key": "n", "label": "N", "kind": "text", "multiple": True}, "several values"),
        ({"key": "Bad Key", "label": "B", "kind": "text"}, "lowercase"),
        ({"key": "x", "label": "X", "kind": "text", "options": ["a"]}, "only select"),
        ({"key": "x", "label": "X", "kind": "colour"}, "kind"),
    ],
)
def test_bad_fields_are_refused_with_a_reason(field: dict[str, Any], message: str) -> None:
    with pytest.raises(TypeProblem, match=message):
        parse_definition({"name": "T", "fields": [field]})


def test_errors_name_fields_for_people() -> None:
    with pytest.raises(TypeProblem, match=r"Field 2 ID: use lowercase"):
        parse_definition({"name": "T", "fields": [
            {"key": "ok", "label": "OK", "kind": "text"},
            {"key": "Bad Key", "label": "Bad", "kind": "text"}]})  # fmt: skip


def test_field_keys_must_be_unique() -> None:
    f = {"key": "a", "label": "A", "kind": "text"}
    with pytest.raises(TypeProblem, match="same ID"):
        parse_definition({"name": "T", "fields": [f, f]})


def test_a_published_fields_kind_cant_change() -> None:
    before = parse_definition(VENDOR)
    after = parse_definition({**VENDOR, "fields": [
        {"key": "account_number", "label": "Account number", "kind": "number"}]})  # fmt: skip
    with pytest.raises(TypeProblem, match="can't change once published"):
        check_compatible(before, after)
    # Relabelling, new fields and removed fields are fine.
    relabelled = parse_definition({"name": "Vendor", "fields": [
        {"key": "account_number", "label": "Customer no.", "kind": "text"},
        {"key": "notes", "label": "Notes", "kind": "longtext"}]})  # fmt: skip
    check_compatible(before, relabelled)


# ---- values -----------------------------------------------------------------------------


def test_values_are_normalized() -> None:
    d = parse_definition(VENDOR)
    out = validate_values(d, {
        "account_number": "  ACME   0042 ",
        "tier": "gold",
        "renews": "2027-03-01",
        "seats": "1,200",
        "portal": "https://acme.example/portal",
        "login": "op://IT/Acme portal/password".replace(" ", "-"),
        "servers": ["assets:device:42", {"module": "assets", "kind": "device", "id": "7"}],
        "support_phone": "",
    })  # fmt: skip
    assert out == {
        "account_number": "ACME 0042",
        "tier": "Gold",
        "renews": "2027-03-01",
        "seats": 1200,
        "portal": "https://acme.example/portal",
        "login": "op://IT/Acme-portal/password",
        "servers": [
            {"module": "assets", "kind": "device", "id": "42"},
            {"module": "assets", "kind": "device", "id": "7"},
        ],
    }  # blanks are dropped


@pytest.mark.parametrize("secret", ["Hunter2!", "correct horse battery staple", "op:/half/wrong"])
def test_credential_fields_refuse_secrets(secret: str) -> None:
    d = parse_definition(VENDOR)
    with pytest.raises(TypeProblem, match="Never the password"):
        validate_values(d, {"account_number": "1", "login": secret})


def test_1password_links_are_accepted() -> None:
    d = parse_definition(VENDOR)
    link = "https://start.1password.com/open/i?a=ABC&v=DEF&i=GHI&h=example.1password.com"
    assert validate_values(d, {"account_number": "1", "login": link})["login"] == link


def test_required_unknown_and_wrong_values() -> None:
    d = parse_definition(VENDOR)
    with pytest.raises(TypeProblem, match="Account number is required"):
        validate_values(d, {})
    assert validate_values(d, {}, require=False) == {}  # drafts may be incomplete
    with pytest.raises(TypeProblem, match="Unknown field"):
        validate_values(d, {"account_number": "1", "colour": "red"})
    with pytest.raises(TypeProblem, match="choose one of Gold, Silver"):
        validate_values(d, {"account_number": "1", "tier": "Bronze"})
    with pytest.raises(TypeProblem, match=r"must link to assets.device"):
        validate_values(d, {"account_number": "1", "servers": "docs:document:1"})
    with pytest.raises(TypeProblem, match="only one value"):
        validate_values(d, {"account_number": ["1", "2"]})


# ---- manifest opt-in ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("perms", "message"),
    [
        ({"m1.types.draft": "Draft"}, "declare both"),
        ({"m1.types.draft": "D", "m1.types.approve": "Approve"}, "must be human_only"),
        ({"m1.types.draft": {"description": "D", "human_only": True},
          "m1.types.approve": {"description": "A", "human_only": True}}, "must not be human_only"),
    ],
)  # fmt: skip
def test_types_opt_in_must_be_complete_and_safe(perms: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Manifest.model_validate({"name": "m1", "version": "0.1.0", "permissions": perms})


# ---- store: draft -> publish ------------------------------------------------------------------


@asynccontextmanager
async def store(database_url: str) -> AsyncIterator[tuple[TypeStore, Any]]:
    engine = make_engine(database_url)
    await migrations.upgrade_core(engine)
    maker = make_sessionmaker(engine)
    async with maker() as s:
        await s.execute(text("DELETE FROM itops.record_types WHERE module = 'tdocs'"))
        await s.commit()
    try:
        yield TypeStore(), maker
    finally:
        auth.set_current(None)
        await engine.dispose()


async def test_ai_drafts_a_person_publishes(database_url: str) -> None:
    async with store(database_url) as (types, maker):
        auth.set_current(ADMIN_AI)
        async with maker() as s:
            view = await types.draft(s, "tdocs", VENDOR, note="from the IT Glue export")
            await s.commit()
        assert (view.key, view.status, view.current.version) == ("vendor", "draft", 1)
        assert view.current.via_ai
        async with maker() as s:
            assert await types.list_types(s, "tdocs") == []  # not usable until published
            with pytest.raises(auth.PermissionDenied):
                await types.publish(s, "tdocs", "vendor")  # an AI never publishes

        auth.set_current(ADMIN)
        async with maker() as s:
            view = await types.publish(s, "tdocs", "vendor")
            await s.commit()
        assert view.status == "published" and view.published and view.published.published_by
        async with maker() as s:
            assert [v.key for v in await types.list_types(s, "tdocs")] == ["vendor"]
            definition = await types.definition(s, "tdocs", "vendor")
        assert definition.name == "Vendor"


async def test_changing_a_published_type(database_url: str) -> None:
    async with store(database_url) as (types, maker):
        auth.set_current(ADMIN)
        async with maker() as s:
            await types.draft(s, "tdocs", VENDOR)
            await types.publish(s, "tdocs", "vendor")
            changed = {**VENDOR, "fields": [*VENDOR["fields"],
                       {"key": "notes", "label": "Notes", "kind": "longtext"}]}  # fmt: skip
            view = await types.draft(s, "tdocs", changed, key="vendor")
            assert view.status == "published, with draft changes"
            assert view.draft and view.draft.version == 2
            # Records keep validating against the published version until it's published.
            assert len((await types.definition(s, "tdocs", "vendor")).fields) == 8
            # A second save replaces the pending draft instead of piling up versions.
            view = await types.draft(s, "tdocs", changed, key="vendor", note="second try")
            assert view.draft and view.draft.version == 2 and view.draft.note == "second try"
            await types.publish(s, "tdocs", "vendor")
            assert len((await types.definition(s, "tdocs", "vendor")).fields) == 9
            # Kind changes are refused even as drafts.
            bad = {**VENDOR, "fields": [{"key": "seats", "label": "Seats", "kind": "text"}]}
            with pytest.raises(TypeProblem, match="can't change"):
                await types.draft(s, "tdocs", bad, key="vendor")
            await s.commit()
            assert [v.version for v in await types.history(s, "tdocs", "vendor")] == [2, 1]


async def test_discard_and_retire(database_url: str) -> None:
    async with store(database_url) as (types, maker):
        auth.set_current(ADMIN)
        async with maker() as s:
            await types.draft(s, "tdocs", {"name": "Scratch"})
            assert await types.discard(s, "tdocs", "scratch") == "deleted"
            assert await types.get(s, "tdocs", "scratch") is None
            await types.draft(s, "tdocs", VENDOR)
            await types.publish(s, "tdocs", "vendor")
            await types.set_retired(s, "tdocs", "vendor", True)
            assert await types.list_types(s, "tdocs") == []  # not offered for new records
            assert (await types.definition(s, "tdocs", "vendor")).name == "Vendor"  # still readable
            await s.commit()


async def test_only_admins_draft_or_approve(database_url: str) -> None:
    async with store(database_url) as (types, maker):
        auth.set_current(PUBLISHER)  # can publish documents, not types
        async with maker() as s:
            with pytest.raises(auth.PermissionDenied):
                await types.draft(s, "tdocs", VENDOR)


async def test_type_changes_are_audited(database_url: str) -> None:
    async with store(database_url) as (types, maker):
        auth.set_current(ADMIN)
        async with maker() as s:
            await types.draft(s, "tdocs", VENDOR)
            await types.publish(s, "tdocs", "vendor")
            await s.commit()
            rows = await s.execute(
                text(
                    "SELECT action FROM itops.audit_log WHERE module = 'tdocs'"
                    " AND entity_id = 'vendor' ORDER BY id DESC"
                )
            )
            assert [r[0] for r in rows.all()][:2] == ["type.published", "type.drafted"]


# ---- MCP tools ----------------------------------------------------------------------------


async def test_ai_drafts_types_over_mcp(database_url: str, make_module: MakeModule) -> None:
    from itops.core.app import create_app
    from itops.core.config import Settings
    from itops.testing.app import McpClient, ToolError

    mod = make_module("tytools", manifest_extra="""
[permissions]
"tytools.read" = "Read"
"tytools.types.draft" = "Draft record types"
"tytools.types.approve" = { description = "Publish record types", human_only = true }
""")  # fmt: skip
    app = create_app(Settings(database_url_override=database_url), modules=[mod])
    async with app.router.lifespan_context(app):
        maker = app.state.runtime.sessionmaker
        async with maker() as s:
            await s.execute(text("DELETE FROM itops.record_types WHERE module = 'tytools'"))
            await s.commit()
        mcp = McpClient([mod], app=app)
        assert {"itops_list_types", "itops_get_type", "itops_draft_type"} <= set(
            await mcp.tool_names()
        )
        made = await mcp.call("itops_draft_type", module="tytools", **VENDOR)
        assert made["status"] == "draft" and made["review_url"].endswith("/tytools/types/vendor")
        assert await mcp.call("itops_list_types", module="tytools") == []
        drafts = await mcp.call("itops_list_types", module="tytools", include_drafts=True)
        assert drafts[0]["key"] == "vendor" and drafts[0]["fields"] == 8
        with pytest.raises(ToolError, match="needs options"):
            await mcp.call("itops_draft_type", module="tytools", name="Bad",
                           fields=[{"key": "t", "label": "T", "kind": "select"}])  # fmt: skip
        with pytest.raises(ToolError, match="has no record types"):
            await mcp.call("itops_list_types", module="nope")


# ---- pages ---------------------------------------------------------------------------------


async def test_type_pages_end_to_end(database_url: str, make_module: MakeModule) -> None:
    import json

    import httpx

    from itops.core.app import create_app
    from itops.core.config import Settings
    from itops.testing.app import acting_as

    mod = make_module("typages", manifest_extra="""
title = "Typages"
[permissions]
"typages.read" = "Read"
"typages.types.draft" = "Draft record types"
"typages.types.approve" = { description = "Publish record types", human_only = true }
""")  # fmt: skip
    app = create_app(Settings(database_url_override=database_url), modules=[mod])
    async with app.router.lifespan_context(app):
        async with app.state.runtime.sessionmaker() as s:
            await s.execute(text("DELETE FROM itops.record_types WHERE module = 'typages'"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            assert "No types yet" in (await c.get("/typages/types")).text
            assert 'href="/typages/types"' in (await c.get("/account")).text

            form = {
                "name": "Vendor",
                "description": "Suppliers",
                "fields": json.dumps(VENDOR["fields"]),
            }
            r = await c.post("/api/typages/types", data=form)
            assert r.status_code == 303 and r.headers["location"] == "/typages/types/vendor"
            page = (await c.get("/typages/types/vendor")).text
            assert (
                "New type with 8 field(s)" in page and "/api/typages/types/vendor/publish" in page
            )

            assert (await c.post("/api/typages/types/vendor/publish")).status_code == 303
            page = (await c.get("/typages/types/vendor")).text
            assert "Published version 1" in page

            # Editing: published keys are locked; a kind change is refused, the form kept.
            assert "account_number" in (await c.get("/typages/types/vendor/edit")).text
            bad = [{"key": "seats", "label": "Seats", "kind": "text"}]
            r = await c.post("/api/typages/types/vendor/draft",
                             data={**form, "fields": json.dumps(bad)})  # fmt: skip
            assert r.status_code == 400 and "can&#39;t change once published" in r.text
            assert "Suppliers" in r.text  # what they typed is still there

            relabelled = [{**f, "label": "Customer no."} if f["key"] == "account_number" else f
                          for f in VENDOR["fields"]]  # fmt: skip
            await c.post("/api/typages/types/vendor/draft",
                         data={**form, "fields": json.dumps(relabelled)})  # fmt: skip
            page = (await c.get("/typages/types/vendor")).text
            assert "Relabels “Account number” to “Customer no.”" in page

            # A query parameter can't turn "discard" into another action.
            r = await c.post("/api/typages/types/vendor/discard?_action=publish&action=publish")
            assert r.status_code == 303
            async with app.state.runtime.sessionmaker() as s:
                view = await app.state.runtime.types.get(s, "typages", "vendor")
            assert view and view.draft is None and view.published and view.published.version == 1

            await c.post("/api/typages/types/vendor/retire")
            page = (await c.get("/typages/types/vendor")).text
            assert "Retired" in page and "All Typages types" in page  # back to the list
            assert "Back to Vendor" in (await c.get("/typages/types/vendor/edit")).text

            # Retired types stay out of the list unless asked for.
            listing = (await c.get("/typages/types")).text
            assert "/typages/types/vendor" not in listing and "Show retired types (1)" in listing
            listing = (await c.get("/typages/types?retired=1")).text
            assert "/typages/types/vendor" in listing and "Hide retired types" in listing

            with acting_as(PUBLISHER):  # not an admin
                assert (await c.get("/typages/types")).status_code == 403

            # Publish all drafts at once: each is published (and audited) as if one by one.
            for name in ("Licence", "Contract"):
                await c.post("/api/typages/types", data={**form, "name": name})
            page = (await c.get("/typages/types")).text
            assert "Publish all drafts (2)" in page
            with acting_as(replace(ADMIN, via_ai=True, client="api")):  # never via AI
                assert (await c.post("/api/typages/types/publish-drafts")).status_code == 403
            r = await c.post("/api/typages/types/publish-drafts", follow_redirects=False)
            assert r.headers["location"] == "/typages/types?published=2"
            page = (await c.get(r.headers["location"])).text
            assert "Published 2 types." in page and "Publish all drafts" not in page


async def test_type_names_are_unique_per_module(database_url: str) -> None:
    async with store(database_url) as (types, maker):
        auth.set_current(ADMIN)
        async with maker() as s:
            await types.draft(s, "tdocs", VENDOR)
            with pytest.raises(TypeProblem, match="already a type named"):
                await types.draft(s, "tdocs", {**VENDOR, "name": "vendor"})
            await types.draft(s, "tdocs", VENDOR, key="vendor", note="same type, same name: fine")
            await s.commit()
