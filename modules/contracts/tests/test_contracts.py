"""Contracts (ADR 0014): terms, renewals and their reminders, costs, and what AI may do."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from itops_contracts import module
from itops_contracts import service as contracts
from sqlalchemy import text

from itops.sdk import Principal
from itops.testing import assert_conformant, assert_migrations_roundtrip
from itops.testing.app import McpClient, ToolError, acting_as, make_app

ADMIN = Principal("admin@example.com", "Admin", frozenset({"*"}))
APPROVER = Principal(
    "paul@example.com",
    "Paul",
    frozenset({"contracts.read", "contracts.write", "contracts.approve", "contracts.costs"}),
)
WRITER = Principal("tech@example.com", "Tech", frozenset({"contracts.read", "contracts.write"}))
READER = Principal("reader@example.com", "Reader", frozenset({"docs.read"}))  # no contracts


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, FastAPI]]:
    app = make_app(database_url, [module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with module.session() as s:
            await s.execute(text("DELETE FROM contracts.terms"))
            await s.execute(text("DELETE FROM contracts.contracts"))
            await s.execute(text("DELETE FROM itops.search_passages WHERE module = 'contracts'"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c, app


def contract_id(r: httpx.Response) -> str:
    m = re.match(r"/contracts/c/([0-9a-f-]+)$", r.headers["location"])
    assert m, r.headers["location"]
    return m[1]


def days(n: int) -> str:
    return str(dt.date.today() + dt.timedelta(days=n))


def new_contract(**extra: str) -> dict[str, str]:
    return {"name": "Sophos endpoint", "vendor": "Sophos", "category": "software",
            "notice_days": "30", "auto_renews": "1", "starts_on": days(-265), "ends_on": days(100),
            "cost": "12,000", "cost_period": "year", "seats_bought": "100", "seats_in_use": "120",
            **extra}  # fmt: skip


def test_conformance() -> None:
    assert_conformant(module)


async def test_migrations_roundtrip(database_url: str) -> None:
    await assert_migrations_roundtrip(module, database_url)


async def test_a_contract_shows_its_term_and_costs_only_to_who_may_see_them(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (c, _):
        with acting_as(APPROVER):
            r = await c.post("/api/contracts/contracts", data=new_contract())
            cid = contract_id(r)
            page = (await c.get(f"/contracts/c/{cid}")).text
            assert "Sophos endpoint" in page and "$12,000.00 per year" in page
            assert days(70) in page  # cancel by: end minus 30 days' notice
            assert "20 more seats in use than bought" in page
            assert "$12,000.00" in (await c.get("/contracts/costs")).text
        with acting_as(WRITER):  # sees the contract, not what it costs
            page = (await c.get(f"/contracts/c/{cid}")).text
            assert "Sophos endpoint" in page and "12,000" not in page
            assert (await c.get("/contracts/costs")).status_code == 403
            # Saving the term without the cost field keeps the cost.
            async with module.session() as s:
                t = await contracts.current_term(s, uuid.UUID(cid))
                assert t is not None
                term_id = t.id
            r = await c.post(f"/api/contracts/terms/{term_id}", data={
                "starts_on": days(-265), "ends_on": days(100), "po_number": "PO-7"})  # fmt: skip
            assert "error" not in r.headers["location"]
        async with module.session() as s:
            t = await contracts.get_term(s, term_id)
            assert t is not None and t.cost == 12000 and t.po_number == "PO-7"
        with acting_as(READER):  # contracts are granted by group: a plain reader gets nothing
            assert (await c.get("/contracts/")).status_code == 403
            assert (await c.get(f"/contracts/c/{cid}")).status_code == 403


async def test_renewal_reminders_count_down_to_the_cancel_by_date(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (c, app):
        with acting_as(APPROVER):
            cid = contract_id(await c.post("/api/contracts/contracts", data=new_contract()))
        cancel_by = dt.date.today() + dt.timedelta(days=70)

        async def check(on: dt.date) -> dict[str, int]:
            async with module.session() as s:
                counts = await contracts.check_renewals(s, on)
                await s.commit()
                return counts

        async def term() -> contracts.Term:
            async with module.session() as s:
                t = await contracts.current_term(s, uuid.UUID(cid))
                assert t is not None
                return t

        # 70 days out: inside the 120-day window, past the 120 and 90 reminders (one event).
        assert await check(dt.date.today()) == {"due": 1, "reminded": 1, "overdue": 0, "lapsed": 0}
        assert (await term()).state == "due" and (await term()).notified == "120,90"
        assert (await check(dt.date.today()))["reminded"] == 0  # once per reminder
        assert (await check(cancel_by - dt.timedelta(days=60)))["reminded"] == 1
        assert (await check(cancel_by - dt.timedelta(days=30)))["reminded"] == 1
        assert (await term()).notified == "120,90,60,30"
        # Past cancel-by, undecided: overdue, repeated weekly.
        assert (await check(cancel_by + dt.timedelta(days=1)))["overdue"] == 1
        assert (await check(cancel_by + dt.timedelta(days=3)))["overdue"] == 0
        assert (await check(cancel_by + dt.timedelta(days=8)))["overdue"] == 1
        # It auto-renews, so it doesn't lapse at the end; a person records the renewal.
        assert (await check(cancel_by + dt.timedelta(days=40)))["lapsed"] == 0
        # The scheduled job runs the same check as itself.
        jobs = app.state.runtime.jobs
        await jobs.run("contracts.renewals")
        await jobs.wait("contracts.renewals")
        assert (await jobs.last_runs())["contracts.renewals"].status == "succeeded"


async def test_a_term_that_does_not_auto_renew_lapses_when_nobody_decides(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (c, _):
        with acting_as(APPROVER):
            cid = contract_id(await c.post("/api/contracts/contracts",
                                           data=new_contract(auto_renews="")))  # fmt: skip
        async with module.session() as s:
            counts = await contracts.check_renewals(s, dt.date.today() + dt.timedelta(days=101))
            await s.commit()
            t = await contracts.current_term(s, uuid.UUID(cid))
        assert counts["lapsed"] == 1 and t is not None and t.state == "lapsed"


async def test_renewing_and_cancelling_are_decided_by_approvers(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (c, _):
        with acting_as(APPROVER):
            cid = contract_id(await c.post("/api/contracts/contracts", data=new_contract()))
        with acting_as(WRITER):
            r = await c.post(f"/api/contracts/contracts/{cid}/renew",
                             data={"starts_on": days(101), "ends_on": days(466)})  # fmt: skip
            assert r.status_code == 403
            await c.post(f"/api/contracts/contracts/{cid}/negotiate")
        with acting_as(APPROVER):
            renew_page = (await c.get(f"/contracts/c/{cid}/renew")).text
            assert days(101) in renew_page and days(465) in renew_page  # same length again
            r = await c.post(f"/api/contracts/contracts/{cid}/renew", data={
                "starts_on": days(101), "ends_on": days(466), "cost": "11000",
                "cost_period": "year", "note": "8% off"})  # fmt: skip
            assert "error" not in r.headers["location"]
        async with module.session() as s:
            history = await contracts.terms(s, uuid.UUID(cid))
        assert [t.state for t in history] == ["active", "renewed"]
        assert history[1].decided_by == APPROVER.user and history[1].decision_note == "8% off"
        with acting_as(APPROVER):
            await c.post(
                f"/api/contracts/contracts/{cid}/cancel", data={"note": "Moving to Defender"}
            )
        async with module.session() as s:
            current = await contracts.current_term(s, uuid.UUID(cid))
        assert current is not None and current.state == "cancelled"


async def test_ai_drafts_contracts_never_sees_costs_and_never_decides(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (c, _):
        with acting_as(APPROVER):
            cid = contract_id(await c.post("/api/contracts/contracts", data=new_contract()))
        mcp = McpClient([module])
        with acting_as(APPROVER):  # the approver's own AI connection
            listed = await mcp.call("contracts_list_contracts")
            assert [x["name"] for x in listed] == ["Sophos endpoint"]
            full = await mcp.call("contracts_get_contract", contract_id=cid)
            assert "cost" not in str(full).lower() and full["cancel_by"] == days(70)
            due = await mcp.call("contracts_renewals_due")
            assert due[0]["days_left"] == 70
            made = await mcp.call(
                "contracts_draft_contract", name="Masergy SD-WAN", category="telecom",
                starts_on=days(-30), ends_on=days(700), vendor="Masergy", notice_days=90,
            )  # fmt: skip
            assert made["status"] == "draft"
            assert [x["name"] for x in await mcp.call("contracts_list_contracts")] == [
                "Sophos endpoint"
            ]  # drafts aren't contracts yet
            changed = await mcp.call("contracts_update_draft", contract_id=made["id"],
                                     notice_days=60)  # fmt: skip
            assert changed["cancel_by"] == days(640)
            with pytest.raises(ToolError, match="accepted"):
                await mcp.call("contracts_update_draft", contract_id=cid, notice_days=1)
        async with module.session() as s:
            draft = await contracts.get(s, made["id"])
            assert draft is not None
            with pytest.raises(contracts.ContractsError, match="AI connection"):
                await contracts.accept(s, replace(APPROVER, via_ai=True), draft)
            await contracts.accept(s, APPROVER, draft)  # a person accepts it
            await s.commit()
        with acting_as(APPROVER):
            names = [x["name"] for x in await mcp.call("contracts_list_contracts")]
        assert names == ["Masergy SD-WAN", "Sophos endpoint"]
        assert sorted(await mcp.tool_names()) == [
            "contracts_draft_contract", "contracts_get_contract", "contracts_list_contracts",
            "contracts_renewals_due", "contracts_update_draft",
        ]  # fmt: skip


class FakeLLM:
    name = "fake-7b"

    async def complete(self, prompt: str, **_: object) -> str:
        return (
            '{"ends_on": {"value": "2027-05-16", "quote": "Expiry Date 05/16/2027"},'
            ' "starts_on": {"value": "2026-01-01", "quote": "Expiry Date 05/16/2027"},'
            ' "cost": {"value": 14250, "quote": "Total $14,250.00"},'
            ' "notice_days": {"value": 30, "quote": "thirty (30) days written notice"}}'
        )


async def test_suggestions_from_a_file_are_reviewed_and_saved_by_a_person(
    database_url: str, tmp_path: Path
) -> None:
    async with running(database_url, tmp_path) as (c, app):
        runtime = app.state.runtime
        with acting_as(APPROVER):
            cid = contract_id(await c.post("/api/contracts/contracts", data=new_contract()))
            assert "Suggest values from" not in (await c.get(f"/contracts/c/{cid}")).text  # off
        runtime.ai._llm = FakeLLM()  # pyright: ignore[reportPrivateUsage]
        runtime.settings.ai_llm_features = ["contracts.suggest"]
        async with module.session() as s:
            t = await contracts.current_term(s, uuid.UUID(cid))
            assert t is not None
            term_id = t.id
        text_file = (
            b"Schedule\nExpiry Date 05/16/2027\nTotal $14,250.00\nthirty (30) days written notice"
        )
        with acting_as(APPROVER):
            await c.post(f"/api/contracts/terms/{term_id}", data={
                "starts_on": days(-265), "ends_on": days(100), "cost": "12000"},
                files={"attachments": ("schedule.txt", text_file, "text/plain")})  # fmt: skip
            async with module.session() as s:
                t = await contracts.get_term(s, term_id)
                assert t is not None
                file_id = contracts.term_attachments(t)[0]["id"]
            assert "Suggest values from schedule.txt" in (await c.get(f"/contracts/c/{cid}")).text
            r = await c.post(f"/api/contracts/terms/{term_id}/suggest/{file_id}")
            review = r.headers["location"]
            assert review.startswith(f"/contracts/t/{term_id}/suggestions/")
            await runtime.suggestions.wait()
            page = (await c.get(review)).text
            assert "Expiry Date 05/16/2027" in page and "data-doc-viewer" in page
            assert "2026-01-01" not in page  # its quote doesn't contain it: dropped
            assert "1 more answer(s) were left out" in page
        with acting_as(WRITER):  # no costs permission: no cost suggestion
            assert 'name="value__cost"' not in (await c.get(review)).text
        with acting_as(APPROVER):
            run_id = int(review.rsplit("/", 1)[-1])
            r = await c.post(f"/api/contracts/terms/{term_id}/suggestions/{run_id}", data={
                "accept__ends_on": "1", "value__ends_on": "2027-05-16",
                "value__notice_days": "30"})  # cost not ticked; notice typed  # fmt: skip
            assert r.headers["location"] == f"/contracts/c/{cid}"
        async with module.session() as s:
            t = await contracts.get_term(s, term_id)
            contract = await contracts.get(s, cid)
            assert t is not None and contract is not None
            assert str(t.ends_on) == "2027-05-16" and t.cost == 12000
            assert contract.notice_days == 30
            activity = await module.audit.history(kind="contract", entity_id=cid)
        assert any("Accepted 2 of 3 suggestion(s) from schedule.txt" in (a.summary or "")
                   for a in activity)  # fmt: skip
        run = await module.ai.suggestion_run(run_id)
        assert run is not None and run.status == "reviewed"


class NewContractLLM:
    name = "fake-7b"

    async def complete(self, prompt: str, **_: object) -> str:
        return (
            '{"name": {"value": "Endpoint Protection",'
            ' "quote": "Contoso Endpoint Protection renewal"},'
            ' "vendor": {"value": "Contoso Security",'
            ' "quote": "Contoso Security, Inc. (the Vendor)"},'
            ' "category": {"value": "software",'
            ' "quote": "Contoso Endpoint Protection renewal"},'
            ' "starts_on": {"value": "2026-11-01", "quote": "Term: 11/01/2026 to 10/31/2027"},'
            ' "ends_on": {"value": "2027-10-31", "quote": "Term: 11/01/2026 to 10/31/2027"},'
            ' "cost": {"value": 14250, "quote": "Total $14,250.00"},'
            ' "notice_days": {"value": 30, "quote": "thirty (30) days written notice"}}'
        )


QUOTE = (
    b"Contoso Endpoint Protection renewal\nContoso Security, Inc. (the Vendor)\n"
    b"Term: 11/01/2026 to 10/31/2027\nTotal $14,250.00\nthirty (30) days written notice"
)


async def test_a_new_contract_starts_from_a_file(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (c, app):
        runtime = app.state.runtime
        with acting_as(APPROVER):
            page = (await c.get("/contracts/new")).text
            assert "Start from a file" not in page and 'name="attachments"' in page  # AI off
            # By hand, the New contract form takes files too.
            r = await c.post(
                "/api/contracts/contracts",
                data=new_contract(name="By hand"),
                files={"attachments": ("signed.txt", b"signed", "text/plain")},
            )
            assert "signed.txt" in (await c.get(r.headers["location"])).text
        runtime.ai._llm = NewContractLLM()  # pyright: ignore[reportPrivateUsage]
        runtime.settings.ai_llm_features = ["contracts.suggest"]
        with acting_as(APPROVER):
            assert "Start from a file" in (await c.get("/contracts/new")).text
            r = await c.post(
                "/api/contracts/from-file", files={"file": ("quote.txt", QUOTE, "text/plain")}
            )
            review = r.headers["location"]
            assert review.startswith("/contracts/from-file/")
            await runtime.suggestions.wait()
            page = (await c.get(review)).text
            assert 'value="Endpoint Protection"' in page and 'value="Contoso Security"' in page
            assert 'value="2027-10-31"' in page and 'value="14250"' in page
            assert 'name="notice_days" value=""' in page  # quoted, never pre-filled
            assert "thirty (30) days written notice" in page and "data-doc-viewer" in page
            r = await c.post(
                review.replace("/contracts/", "/api/contracts/", 1),
                data={
                    "name": "Endpoint Protection",
                    "vendor": "Contoso Security",
                    "category": "software",
                    "starts_on": "2026-11-01",
                    "ends_on": "2027-10-31",
                    "cost": "14250",
                    "cost_period": "year",
                    "notice_days": "30",
                    "auto_renews": "1",
                },
            )
            cid = contract_id(r)
            page = (await c.get(f"/contracts/c/{cid}")).text
            assert "quote.txt" in page and "$14,250.00 per year" in page
            # The same file can't make a second contract.
            again = await c.post(
                review.replace("/contracts/", "/api/contracts/", 1),
                data={"name": "Twice", "starts_on": "2026-11-01", "ends_on": "2027-10-31"},
            )
            assert "already used" in again.headers["location"].replace("%20", " ")
        async with module.session() as s:
            contract = await contracts.get(s, cid)
            assert contract is not None and contract.notice_days == 30 and contract.auto_renews
            activity = await module.audit.history(kind="contract", entity_id=cid)
        assert any("read from quote.txt" in (a.summary or "") for a in activity)
