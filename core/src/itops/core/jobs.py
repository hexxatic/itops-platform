"""Scheduled module jobs (ADR 0014): module housekeeping on a schedule.

A module registers ``@module.job("renewals")`` and declares ``[jobs.renewals]`` in its
manifest (title, schedule, permissions). The core runs it on that schedule, one run at a
time, as itself (``job:<module>.<name>``, never a person, never via AI), and records each
run in ``itops.job_runs``. A job is neither a flow of data in or out (a connector, ADR
0010) nor an operational job people run (a tool action, ADR 0011): it only works on its
own module's data, e.g. marking contract terms whose notice window opened and publishing
an event about them. ``async def run() -> str | None`` returns a short summary.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core import auth
from itops.core.connectors import TICK_SECONDS, is_due, next_due, utcnow
from itops.core.manifest import JobDecl

JobFn = Callable[[], Awaitable[str | None]]
log = logging.getLogger("itops.jobs")


@dataclass(frozen=True)
class RegisteredJob:
    key: str  # "<module>.<name>"
    module: str
    name: str
    decl: JobDecl
    fn: JobFn

    @property
    def principal(self) -> auth.Principal:
        return auth.Principal(
            user=f"job:{self.key}",
            display_name=f"{self.decl.title} (scheduled job)",
            permissions=frozenset(self.decl.permissions),
            client="job",
        )


@dataclass(frozen=True)
class JobRun:
    id: int
    job: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    status: str
    summary: str


def _no_tasks() -> dict[str, asyncio.Task[None]]:
    return {}


@dataclass
class JobRunner:
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    _jobs: dict[str, RegisteredJob] = field(default_factory=dict[str, RegisteredJob])
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=_no_tasks)

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        if self.sessionmaker is None:
            raise RuntimeError("jobs have no database")
        return self.sessionmaker

    def register(self, module: str, name: str, decl: JobDecl, fn: JobFn) -> None:
        key = f"{module}.{name}"
        self._jobs[key] = RegisteredJob(key, module, name, decl, fn)

    def all(self) -> list[RegisteredJob]:
        return sorted(self._jobs.values(), key=lambda j: j.key)

    def is_running(self, key: str) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    async def last_runs(self) -> dict[str, JobRun]:
        """Each job's newest run."""
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    "SELECT r.id, r.job, r.started_at, r.finished_at, r.status, r.summary"
                    " FROM itops.job_runs r WHERE r.id IN"
                    " (SELECT MAX(id) FROM itops.job_runs GROUP BY job)"
                )
            )
            return {
                r[1]: JobRun(id=r[0], job=r[1], started_at=r[2], finished_at=r[3], status=r[4],
                             summary=r[5] or "")
                for r in rows.all()
            }  # fmt: skip

    def next_due(self, job: RegisteredJob, last: JobRun | None) -> dt.datetime | None:
        return next_due(job.decl.schedule, last.started_at if last else None, utcnow())

    async def run(self, key: str) -> int:
        """Run a job now (the scheduler does; tests too). Returns the run id."""
        job = self._jobs[key]
        async with self._maker()() as s:
            run_id = int(
                (
                    await s.execute(
                        text(
                            "INSERT INTO itops.job_runs (job, status) OUTPUT inserted.id"
                            " VALUES (:j, 'running')"
                        ),
                        {"j": key},
                    )
                ).scalar_one()
            )
            await s.commit()
        task = asyncio.create_task(self._execute(job, run_id))
        self._tasks[key] = task
        return run_id

    async def wait(self, key: str) -> None:
        task = self._tasks.get(key)
        if task is not None:
            await task

    async def _execute(self, job: RegisteredJob, run_id: int) -> None:
        status, summary = "succeeded", ""
        token = auth.set_current_token(job.principal)
        try:
            summary = await job.fn() or ""
        except Exception as exc:
            status, summary = "failed", f"Stopped: {exc}"
            log.exception("job %s run %s failed", job.key, run_id)
        finally:
            auth.reset_current(token)
            async with self._maker()() as s:
                await s.execute(
                    text(
                        "UPDATE itops.job_runs SET finished_at = sysutcdatetime(), status = :st,"
                        " summary = :sm WHERE id = :i"
                    ),
                    {"st": status, "sm": summary[:500], "i": run_id},
                )
                await s.commit()

    async def recover(self) -> int:
        """Runs left 'running' by a stopped service are marked failed."""
        async with self._maker()() as s:
            result = await s.execute(
                text(
                    "UPDATE itops.job_runs SET status = 'failed', finished_at = sysutcdatetime(),"
                    " summary = 'Interrupted: the service stopped' WHERE status = 'running'"
                )
            )
            await s.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def tick(self, now: dt.datetime | None = None) -> list[str]:
        """Start every job whose schedule is due; returns their keys."""
        now = now or utcnow()
        last = await self.last_runs()
        started: list[str] = []
        for job in self.all():
            if self.is_running(job.key):
                continue
            prior = last.get(job.key)
            if is_due(job.decl.schedule, prior.started_at if prior else None, now):
                await self.run(job.key)
                started.append(job.key)
        return started

    async def scheduler(self) -> None:
        """Runs for the life of the service (started in the app's lifespan)."""
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("job scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)
