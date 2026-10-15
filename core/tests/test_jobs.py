"""Scheduled module jobs (ADR 0014): declarations, running as themselves, recording runs."""

from __future__ import annotations

import datetime as dt

import pytest
from _helpers import MakeModule
from sqlalchemy import text

from itops.core import auth, migrations
from itops.core.db import make_engine, make_sessionmaker
from itops.core.jobs import JobRunner
from itops.core.loader import validate
from itops.core.manifest import JobDecl, Manifest


@pytest.mark.parametrize("schedule", ["manual", "hourly", "every 5s"])
def test_a_job_needs_a_real_schedule(schedule: str) -> None:
    with pytest.raises(ValueError, match="schedule"):
        JobDecl(title="Renewals", schedule=schedule)


def test_jobs_hold_only_their_own_non_human_permissions() -> None:
    base = {"name": "contracts", "version": "0.1.0", "permissions": {
        "contracts.write": "Edit", "contracts.approve": {"description": "Approve",
                                                         "human_only": True}}}  # fmt: skip
    ok = {"renewals": {"title": "R", "schedule": "daily 06:00", "permissions": ["contracts.write"]}}
    assert Manifest.model_validate({**base, "jobs": ok}).jobs["renewals"].title == "R"
    for perms, message in [(["contracts.approve"], "human-only"), (["docs.write"], "not declared")]:
        bad = {"renewals": {"title": "R", "schedule": "daily 06:00", "permissions": perms}}
        with pytest.raises(ValueError, match=message):
            Manifest.model_validate({**base, "jobs": bad})


def test_declared_and_registered_jobs_must_match(make_module: MakeModule) -> None:
    body = '@module.job("tidy")\nasync def tidy() -> str:\n    return "done"\n'
    declared = '[jobs.tidy]\ntitle = "Tidy"\nschedule = "every 1h"\n'
    assert validate(make_module("jobalpha", manifest_extra=declared, body=body)) == []
    assert any("not declared in [jobs]" in p for p in validate(make_module("jobbeta", body=body)))
    problems = validate(make_module("jobgamma", manifest_extra=declared))
    assert any("no @module.job('tidy')" in p for p in problems)


async def test_jobs_run_as_themselves_on_schedule_and_record_each_run(database_url: str) -> None:
    engine = make_engine(database_url)
    await migrations.upgrade_core(engine)
    maker = make_sessionmaker(engine)
    async with maker() as s:
        await s.execute(text("DELETE FROM itops.job_runs WHERE job LIKE 'tjobs.%'"))
        await s.commit()
    seen: list[auth.Principal | None] = []

    async def tidy() -> str:
        seen.append(auth.current())
        return "Tidied 3"

    async def broken() -> str:
        raise RuntimeError("no database for you")

    runner = JobRunner(maker)
    runner.register("tjobs", "tidy", JobDecl(title="Tidy", schedule="every 1h",
                                             permissions=["tjobs.write"]), tidy)  # fmt: skip
    runner.register("tjobs", "broken", JobDecl(title="Broken", schedule="every 1h"), broken)
    try:
        assert sorted(await runner.tick()) == ["tjobs.broken", "tjobs.tidy"]  # never ran: due
        await runner.wait("tjobs.tidy")
        await runner.wait("tjobs.broken")
        who = seen[0]
        assert who is not None and who.user == "job:tjobs.tidy" and not who.via_ai
        assert who.has("tjobs.write") and not who.has("tjobs.publish")
        last = await runner.last_runs()
        assert (last["tjobs.tidy"].status, last["tjobs.tidy"].summary) == ("succeeded", "Tidied 3")
        assert last["tjobs.broken"].status == "failed"
        assert "no database for you" in last["tjobs.broken"].summary
        assert await runner.tick() == []  # ran just now: not due again for an hour
        later = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(hours=1, minutes=1)
        assert sorted(await runner.tick(later)) == ["tjobs.broken", "tjobs.tidy"]
        await runner.wait("tjobs.tidy")
        await runner.wait("tjobs.broken")
    finally:
        await engine.dispose()
