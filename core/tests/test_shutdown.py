"""Shutting down and command-line maintenance: runs cut off when IT-Ops stops are recorded
as stopped (never as succeeded), and ``itops search rebuild`` leaves the running service's
runs and schedules alone."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from _helpers import MakeModule, perms
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core import app as app_module
from itops.core import migrations
from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.connectors import ConnectorContext, ConnectorRunner
from itops.core.db import make_engine, make_sessionmaker
from itops.core.jobs import JobRunner
from itops.core.manifest import ConnectorDecl, JobDecl

KEY = "tstop.pull"


@asynccontextmanager
async def database(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(database_url)
    await migrations.upgrade_core(engine)
    maker = make_sessionmaker(engine)
    async with maker() as s:
        for table in ("source_refs", "connector_runs"):
            await s.execute(text(f"DELETE FROM itops.{table} WHERE connector LIKE 'tstop.%'"))
        await s.execute(text("DELETE FROM itops.job_runs WHERE job LIKE 'tstop.%'"))
        await s.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


async def connector_run(maker: async_sessionmaker[AsyncSession], run_id: int) -> Any:
    async with maker() as s:
        return (
            await s.execute(
                text("SELECT status, counts, problems FROM itops.connector_runs WHERE id = :i"),
                {"i": run_id},
            )
        ).one()


async def test_a_connector_run_cut_off_by_shutdown_is_stopped_and_flags_nothing(
    database_url: str,
) -> None:
    started = asyncio.Event()

    async def pull(ctx: ConnectorContext) -> None:
        ctx.count("created")
        ctx.finished("device")  # read completely, but the run never gets to the end
        started.set()
        await asyncio.Event().wait()  # still working when IT-Ops shuts down

    async with database(database_url) as maker:
        async with maker() as s:  # a record an earlier run saw
            await s.execute(
                text(
                    "INSERT INTO itops.source_refs (connector, source_type, source_id, module,"
                    " entity_kind, entity_id, last_run_id) VALUES (:c, 'device', 'D1', 'assets',"
                    " 'record', '1', 0)"
                ),
                {"c": KEY},
            )
            await s.commit()
        runner = ConnectorRunner(maker)
        runner.register("tstop", "pull", ConnectorDecl(title="Pull", system="Somewhere"), pull)
        run_id = await runner.start(KEY, trigger="schedule")
        await started.wait()
        await runner.stop()

        assert not runner.is_running(KEY)
        status, counts, found = await connector_run(maker, run_id)
        assert status == "stopped"
        assert counts == '{"created": 1}'  # what it did before it stopped
        assert "IT-Ops shut down" in found and "nothing after this point was synced" in found
        async with maker() as s:
            missing = (
                await s.execute(
                    text("SELECT missing_since FROM itops.source_refs WHERE connector = :c"),
                    {"c": KEY},
                )
            ).scalar()
        assert missing is None  # a stopped run never flags records as gone


async def test_a_job_cut_off_by_shutdown_is_stopped(database_url: str) -> None:
    started = asyncio.Event()

    async def tidy() -> str:
        started.set()
        await asyncio.Event().wait()
        return "Tidied"

    async with database(database_url) as maker:
        runner = JobRunner(maker)
        runner.register("tstop", "tidy", JobDecl(title="Tidy", schedule="every 1h"), tidy)
        await runner.run("tstop.tidy")
        await started.wait()
        await runner.stop()
        last = (await runner.last_runs())["tstop.tidy"]
        assert last.status == "stopped" and "IT-Ops shut down" in last.summary


BODY = """
import asyncio
from itops.sdk import ConnectorContext

STARTED = asyncio.Event()

@module.connector("pull")
async def pull(ctx: ConnectorContext) -> None:
    STARTED.set()
    await asyncio.Event().wait()

@module.job("tidy")
async def tidy() -> str:
    return "Tidied"
"""
DECLARED = (
    perms("tstop")
    + '[connectors.pull]\ntitle = "Pull"\nsystem = "Somewhere"\nschedule = "every 1m"\n'
    + '[jobs.tidy]\ntitle = "Tidy"\nschedule = "every 1h"\n'
)


async def test_shutting_down_stops_the_runs_still_going(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("tstop", manifest_extra=DECLARED, body=BODY)
    settings = Settings(database_url_override=database_url, files_dir=str(tmp_path),
                        run_connectors=False, run_jobs=False, reindex_on_start=False)  # fmt: skip
    async with database(database_url) as maker:
        app = create_app(settings, modules=[mod])
        async with app.router.lifespan_context(app):
            run_id = await app.state.runtime.connectors.start(KEY, trigger="schedule")
            await sys.modules[mod.package].STARTED.wait()  # type: ignore[attr-defined]
        status, _counts, found = await connector_run(maker, run_id)
        assert status == "stopped" and "IT-Ops shut down" in found


@pytest.mark.parametrize("background", [True, False])
async def test_command_line_maintenance_leaves_the_service_alone(
    make_module: MakeModule,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background: bool,
) -> None:
    """``itops search rebuild`` (``background=False``) runs beside the service: it mustn't
    mark the service's runs interrupted, start the schedulers or re-index on its own."""
    name = f"tstop{int(background)}"
    mod = make_module(name, manifest_extra=DECLARED.replace("tstop", name), body=BODY)
    settings = Settings(database_url_override=database_url, files_dir=str(tmp_path))
    started: list[str] = []

    async def scheduler() -> None:
        started.append("scheduler")

    async def reindex(*_args: Any) -> None:
        started.append("reindex")

    async with database(database_url) as maker:
        async with maker() as s:  # a run the service has going
            await s.execute(
                text(
                    "INSERT INTO itops.connector_runs (connector, [trigger], dry_run, started_by,"
                    " status) VALUES ('tstop.live', 'schedule', 0, 'schedule', 'running')"
                )
            )
            await s.commit()
        app = create_app(settings, modules=[mod], background=background)
        runtime = app.state.runtime
        monkeypatch.setattr(runtime.connectors, "scheduler", scheduler)
        monkeypatch.setattr(runtime.jobs, "scheduler", scheduler)
        monkeypatch.setattr(app_module, "_reindex_changed", reindex)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)  # let anything started get going
        async with maker() as s:
            live = (
                await s.execute(
                    text("SELECT status FROM itops.connector_runs WHERE connector = 'tstop.live'")
                )
            ).scalar()
            await s.execute(text("DELETE FROM itops.connector_runs WHERE connector = 'tstop.live'"))
            await s.commit()
    if background:  # the service itself: recovers, schedules and re-indexes
        assert live == "failed" and sorted(started) == ["reindex", "scheduler", "scheduler"]
    else:
        assert live == "running" and started == []
