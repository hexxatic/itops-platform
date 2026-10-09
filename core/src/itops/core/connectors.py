"""Connectors (ADR 0010): every flow of data into or out of IT-Ops.

A module registers a connector (``@module.connector("entra")``) and declares it in its
manifest (``[connectors.entra]``). The core runs it inside the service, on its schedule
or when an admin clicks Run now / Dry run on ``/connectors``, and records every run
(counts, problems). While it runs, the connector acts as itself
(``connector:<module>.<name>``), never as a person, so the audit trail says who changed
what.

Connectors change data only through the owning module's services (e.g.
``assets.upsert``). Those services use :class:`SourceRefs` (``module.refs`` in the SDK)
to remember where each record came from, so a re-run updates instead of duplicating,
skips unchanged records and notices records that disappeared from the source. During a
**dry run** they must not write: :func:`current_run` tells them.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core import auth
from itops.core.audit import AuditLog
from itops.core.manifest import ConnectorDecl

# Only admins hold it: roles grant permissions by their last word (read/write/publish).
CONNECTORS_PERMISSION = "itops.connectors"
MAX_PROBLEMS = 500
PROGRESS_SECONDS = 2.0  # how often a running connector's progress is saved
TICK_SECONDS = 30
log = logging.getLogger("itops.connectors")


class ConnectorError(RuntimeError):
    """A problem that stops a run (missing secret, misconfiguration)."""


HUMAN_ONLY = (
    "Only a person can start a real run or turn a connector on (in the web UI). Tokens and AI "
    "tools can start dry runs."
)


def _require_human() -> None:
    """Real runs write to IT-Ops and scheduling starts them later: a person's deliberate
    click, never a token or an AI tool (they may start dry runs). The page checks this
    first; this is the second lock."""
    who = auth.current()
    if who is not None and who.via_ai:
        raise ConnectorError(HUMAN_ONLY)


def _rowcount(result: Any) -> int:
    """Rows an UPDATE/INSERT touched."""
    return int(getattr(result, "rowcount", 0) or 0)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def fingerprint(values: Any) -> str:
    """A stable hash of source values (unchanged records are skipped)."""
    raw = json.dumps(values, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---- schedules ----------------------------------------------------------------------------

_EVERY_RE = re.compile(r"^every (\d+)([mhd])$")
_DAILY_RE = re.compile(r"^daily (\d{2}):(\d{2})$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def _daily_slot_utc(hour: int, minute: int, now_utc: dt.datetime) -> dt.datetime:
    """Today's HH:MM in the server's local time, as naive UTC."""
    local_now = now_utc.replace(tzinfo=dt.UTC).astimezone()
    slot = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot.astimezone(dt.UTC).replace(tzinfo=None)


def next_due(schedule: str, last_start: dt.datetime | None, now: dt.datetime) -> dt.datetime | None:
    """When a scheduled run is next due (naive UTC); ``None`` for manual connectors.
    A time in the past means "due now"."""
    if m := _EVERY_RE.match(schedule):
        if last_start is None:
            return now
        return last_start + dt.timedelta(**{_UNITS[m[2]]: int(m[1])})
    if m := _DAILY_RE.match(schedule):
        slot = _daily_slot_utc(int(m[1]), int(m[2]), now)
        if last_start is not None and last_start >= slot:
            return slot + dt.timedelta(days=1)
        return slot
    return None


def is_due(schedule: str, last_start: dt.datetime | None, now: dt.datetime) -> bool:
    due = next_due(schedule, last_start, now)
    return due is not None and due <= now


# ---- the current run (read by module services) --------------------------------------------


@dataclass(frozen=True)
class RunInfo:
    connector: str  # "<module>.<name>"
    run_id: int
    dry_run: bool
    owns: dict[str, frozenset[str]]  # "assets.device" -> field keys ("*" = all)


_run: ContextVar[RunInfo | None] = ContextVar("itops_connector_run", default=None)


def current_run() -> RunInfo | None:
    """The connector run the current code is part of, if any."""
    return _run.get()


@contextmanager
def _in_run(info: RunInfo) -> Generator[None]:
    token = _run.set(info)
    try:
        yield
    finally:
        _run.reset(token)


# ---- source references --------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    id: int
    connector: str
    source_type: str
    source_id: str
    module: str
    entity_kind: str
    entity_id: str
    fingerprint: str | None
    first_seen: dt.datetime
    last_seen: dt.datetime
    missing_since: dt.datetime | None


_REF_COLUMNS = (
    "id, connector, source_type, source_id, module, entity_kind, entity_id, fingerprint,"
    " first_seen, last_seen, missing_since"
)


def _ref(row: Any) -> SourceRef:
    return SourceRef(*row)


class SourceRefs:
    """Which record each source record became. Written in the caller's session, so a
    reference commits or rolls back with the record it points at."""

    async def find(
        self, s: AsyncSession, *, connector: str, source_type: str, source_id: str, module: str
    ) -> SourceRef | None:
        row = (
            await s.execute(
                text(
                    f"SELECT {_REF_COLUMNS} FROM itops.source_refs WHERE connector = :c"
                    " AND source_type = :t AND source_id = :i AND module = :m"
                ),
                {"c": connector, "t": source_type, "i": source_id, "m": module},
            )
        ).first()
        return _ref(row) if row else None

    async def link(
        self,
        s: AsyncSession,
        *,
        connector: str,
        source_type: str,
        source_id: str,
        module: str,
        entity_kind: str,
        entity_id: str,
        fingerprint: str | None,
        run_id: int | None,
    ) -> None:
        """Point a source record at an entity (insert, or repoint an existing reference)."""
        params = {
            "c": connector, "t": source_type, "i": source_id[:200], "m": module,
            "k": entity_kind, "e": entity_id, "f": fingerprint, "r": run_id,
        }  # fmt: skip
        updated = await s.execute(
            text(
                "UPDATE itops.source_refs SET entity_kind = :k, entity_id = :e, fingerprint = :f,"
                " last_seen = sysutcdatetime(), last_run_id = :r, missing_since = NULL"
                " WHERE connector = :c AND source_type = :t AND source_id = :i AND module = :m"
            ),
            params,
        )
        if not _rowcount(updated):
            await s.execute(
                text(
                    "INSERT INTO itops.source_refs (connector, source_type, source_id, module,"
                    " entity_kind, entity_id, fingerprint, last_run_id)"
                    " VALUES (:c, :t, :i, :m, :k, :e, :f, :r)"
                ),
                params,
            )

    async def seen(
        self, s: AsyncSession, ref: SourceRef, *, fingerprint: str | None, run_id: int | None
    ) -> None:
        """The source record is still there (and now has this fingerprint)."""
        await s.execute(
            text(
                "UPDATE itops.source_refs SET last_seen = sysutcdatetime(), last_run_id = :r,"
                " missing_since = NULL, fingerprint = :f WHERE id = :id"
            ),
            {"r": run_id, "f": fingerprint, "id": ref.id},
        )

    async def for_entity(
        self, s: AsyncSession, *, module: str, entity_kind: str, entity_id: str
    ) -> list[SourceRef]:
        rows = await s.execute(
            text(
                f"SELECT {_REF_COLUMNS} FROM itops.source_refs WHERE module = :m"
                " AND entity_kind = :k AND entity_id = :e ORDER BY id"
            ),
            {"m": module, "k": entity_kind, "e": entity_id},
        )
        return [_ref(r) for r in rows.all()]

    async def mark_missing(
        self, s: AsyncSession, *, connector: str, source_type: str, run_id: int
    ) -> int:
        """References of ``source_type`` this run didn't see: flag them (returns how many
        were newly flagged)."""
        result = await s.execute(
            text(
                "UPDATE itops.source_refs SET missing_since = sysutcdatetime()"
                " WHERE connector = :c AND source_type = :t AND missing_since IS NULL"
                " AND (last_run_id IS NULL OR last_run_id <> :r)"
            ),
            {"c": connector, "t": source_type, "r": run_id},
        )
        return _rowcount(result)


# ---- what a connector gets ----------------------------------------------------------------

SecretReader = Callable[[str], str | None]


def read_secret(env_name: str) -> str | None:
    """The environment first, then the ``.env`` file the settings read."""
    return os.environ.get(env_name) or dotenv_values(".env").get(env_name)


class ConnectorContext:
    """Handed to a connector's run function: report outcomes, read secrets, say which
    source types were read completely (so missing records can be flagged)."""

    def __init__(
        self, *, key: str, decl: ConnectorDecl, run_id: int, dry_run: bool, secrets: SecretReader
    ) -> None:
        self.key = key
        self.title = decl.title
        self.run_id = run_id
        self.dry_run = dry_run
        self.counts: dict[str, int] = {}
        self.problems: list[dict[str, str]] = []
        self.progress: dict[str, Any] = {}  # step, number, done, total
        self.changes = 0  # bumped on every report, so progress is only saved when it moved
        self._decl = decl
        self._secrets = secrets
        self._complete: set[str] = set()

    def step(self, name: str, *, total: int | None = None) -> None:
        """Start the next step of the run ("Devices"), shown on the run's page while it
        runs. ``total``: how many items the step will process, when known (a progress bar)."""
        number = int(self.progress.get("number", 0)) + 1
        self.progress = {"step": name[:100], "number": number, "done": 0, "total": total}
        self.changes += 1

    def advance(self, n: int = 1) -> None:
        """``n`` more items of the current step are done. ``apply`` does this for you; call
        it for items that end another way (skipped, failed before an upsert)."""
        if self.progress:
            self.progress["done"] = int(self.progress["done"]) + n
        self.changes += 1

    def count(self, outcome: str, n: int = 1) -> None:
        """Count an outcome: created, updated, unchanged, skipped, failed, ..."""
        self.counts[outcome] = self.counts.get(outcome, 0) + n
        self.changes += 1

    def problem(self, message: str, *, item: str | None = None) -> None:
        """Something a person should look at (the run carries on)."""
        self.changes += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append({"item": (item or "")[:300], "text": message[:1000]})
        elif len(self.problems) == MAX_PROBLEMS:
            self.problems.append({"item": "", "text": "More problems were left out."})

    def apply(self, result: dict[str, Any], *, item: str | None = None) -> None:
        """Count an upsert service's result ({outcome, problem?}): one item done."""
        self.count(str(result.get("outcome", "unknown")))
        self.advance()
        if result.get("problem"):
            self.problem(str(result["problem"]), item=item)

    def secret(self, name: str) -> str:
        """A declared secret, from ``ITOPS_<MODULE>_<NAME>`` (never stored in IT-Ops)."""
        if name not in self._decl.secrets:
            raise ConnectorError(f"secret {name!r} isn't declared in the connector's manifest")
        env_name = f"ITOPS_{self.key.split('.', 1)[0]}_{name}".upper()
        value = self._secrets(env_name)
        if not value:
            raise ConnectorError(f"{env_name} isn't set on this server")
        return value

    def finished(self, source_type: str) -> None:
        """Every record of ``source_type`` was read: anything not seen this run is gone
        from the source (flagged for review unless the connector says ``missing =
        "ignore"``)."""
        self._complete.add(source_type)


ConnectorFn = Callable[[ConnectorContext], Awaitable[None]]


# ---- runs ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    id: int
    connector: str
    trigger: str
    dry_run: bool
    started_by: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    status: str
    counts: dict[str, int]
    problems: list[dict[str, str]]
    summary: str
    progress: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def duration(self) -> str:
        """How long it took; while it runs, how long it has been running."""
        end = self.finished_at or (utcnow() if self.status == "running" else None)
        if end is None:
            return ""
        secs = max(0, int((end - self.started_at).total_seconds()))
        return f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"


_RUN_COLUMNS = (
    "id, connector, [trigger], dry_run, started_by, started_at, finished_at, status, counts,"
    " problems, summary, progress"
)


def _run_row(row: Any) -> Run:
    return Run(
        id=row[0], connector=row[1], trigger=row[2], dry_run=bool(row[3]), started_by=row[4],
        started_at=row[5], finished_at=row[6], status=row[7],
        counts=json.loads(row[8]) if row[8] else {},
        problems=json.loads(row[9]) if row[9] else [], summary=row[10] or "",
        progress=json.loads(row[11]) if row[11] else {},
    )  # fmt: skip


def summarize(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "nothing to do"


@dataclass
class Registered:
    key: str  # "<module>.<name>"
    module: str
    name: str
    decl: ConnectorDecl
    fn: ConnectorFn

    @property
    def principal(self) -> auth.Principal:
        return auth.Principal(
            user=f"connector:{self.key}",
            display_name=f"{self.decl.title} (connector)",
            permissions=frozenset(self.decl.permissions),
            client="connector",
        )

    @property
    def owns(self) -> dict[str, frozenset[str]]:
        return {kind: frozenset(keys) for kind, keys in self.decl.owns.items()}


def _no_tasks() -> dict[str, asyncio.Task[None]]:
    return {}


@dataclass
class ConnectorRunner:
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    audit: AuditLog = field(default_factory=AuditLog)
    refs: SourceRefs = field(default_factory=SourceRefs)
    secrets: SecretReader = read_secret
    _connectors: dict[str, Registered] = field(default_factory=dict[str, Registered])
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=lambda: _no_tasks())

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        assert self.sessionmaker is not None, "connectors need a database"
        return self.sessionmaker

    # -- registry

    def register(self, module: str, name: str, decl: ConnectorDecl, fn: ConnectorFn) -> None:
        key = f"{module}.{name}"
        self._connectors[key] = Registered(key, module, name, decl, fn)

    def all(self) -> list[Registered]:
        return sorted(self._connectors.values(), key=lambda r: r.decl.title.lower())

    def get(self, key: str) -> Registered | None:
        return self._connectors.get(key)

    def owned_fields(self, connector: str, kind: str) -> frozenset[str]:
        """Field keys ``connector`` owns on records of ``kind`` ("assets.device")."""
        reg = self._connectors.get(connector)
        return frozenset(reg.decl.owns.get(kind, [])) if reg else frozenset()

    # -- state

    async def enabled(self) -> dict[str, bool]:
        async with self._maker()() as s:
            rows = await s.execute(text("SELECT connector, enabled FROM itops.connector_state"))
            return {k: bool(e) for k, e in rows.all()}

    async def set_enabled(self, key: str, enabled: bool) -> None:
        reg = self._connectors[key]
        if enabled:
            _require_human()
        who = auth.current()
        async with self._maker()() as s:
            params = {"k": key, "e": enabled, "b": who.user if who else "system"}
            changed = await s.execute(
                text(
                    "UPDATE itops.connector_state SET enabled = :e, changed_by = :b,"
                    " changed_at = sysutcdatetime() WHERE connector = :k"
                ),
                params,
            )
            if not _rowcount(changed):
                await s.execute(
                    text(
                        "INSERT INTO itops.connector_state (connector, enabled, changed_by)"
                        " VALUES (:k, :e, :b)"
                    ),
                    params,
                )
            verb = "enabled" if enabled else "disabled"
            await self.audit.record(
                s, module="itops", action=f"connector.{verb}", entity_kind="connector",
                entity_id=key, title=reg.decl.title,
                summary=f"{verb.capitalize()} {reg.decl.title}",
            )  # fmt: skip
            await s.commit()

    # -- runs

    async def runs(self, key: str, *, limit: int = 30) -> list[Run]:
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    f"SELECT TOP (:n) {_RUN_COLUMNS} FROM itops.connector_runs"
                    " WHERE connector = :k ORDER BY id DESC"
                ),
                {"n": limit, "k": key},
            )
            return [_run_row(r) for r in rows.all()]

    async def run(self, run_id: int) -> Run | None:
        async with self._maker()() as s:
            row = (
                await s.execute(
                    text(f"SELECT {_RUN_COLUMNS} FROM itops.connector_runs WHERE id = :i"),
                    {"i": run_id},
                )
            ).first()
            return _run_row(row) if row else None

    async def last_runs(self) -> dict[str, Run]:
        """Each connector's newest run."""
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    f"SELECT {_RUN_COLUMNS} FROM itops.connector_runs WHERE id IN"
                    " (SELECT MAX(id) FROM itops.connector_runs GROUP BY connector)"
                )
            )
            return {r.connector: r for r in (_run_row(x) for x in rows.all())}

    async def last_scheduled_start(self, key: str) -> dt.datetime | None:
        async with self._maker()() as s:
            return (
                await s.execute(
                    text(
                        "SELECT MAX(started_at) FROM itops.connector_runs"
                        " WHERE connector = :k AND [trigger] = 'schedule'"
                    ),
                    {"k": key},
                )
            ).scalar()

    def is_running(self, key: str) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    async def start(self, key: str, *, trigger: str = "manual", dry_run: bool = False) -> int:
        """Start a run in the background; returns its id. One run at a time per connector."""
        reg = self._connectors.get(key)
        if reg is None:
            raise ConnectorError(f"no connector {key!r}")
        if self.is_running(key):
            raise ConnectorError(f"{reg.decl.title} is already running")
        if trigger == "manual" and not dry_run:
            _require_human()
        who = auth.current()
        started_by = "schedule" if trigger == "schedule" else (who.user if who else "system")
        async with self._maker()() as s:
            run_id = int(
                (
                    await s.execute(
                        text(
                            "INSERT INTO itops.connector_runs (connector, [trigger], dry_run,"
                            " started_by, status) OUTPUT inserted.id"
                            " VALUES (:k, :t, :d, :b, 'running')"
                        ),
                        {"k": key, "t": trigger, "d": dry_run, "b": started_by},
                    )
                ).scalar_one()
            )
            if trigger == "manual":
                kind = "dry run" if dry_run else "run"
                await self.audit.record(
                    s, module="itops", action="connector.run_started", entity_kind="connector",
                    entity_id=key, title=reg.decl.title,
                    summary=f"Started a {kind} of {reg.decl.title}", details={"run": run_id},
                )  # fmt: skip
            await s.commit()
        self._tasks[key] = asyncio.create_task(self._execute(reg, run_id, dry_run))
        return run_id

    async def wait(self, key: str) -> None:
        """Wait for a connector's current run to finish (tests, shutdown)."""
        task = self._tasks.get(key)
        if task is not None:
            await task

    async def _execute(self, reg: Registered, run_id: int, dry_run: bool) -> None:
        ctx = ConnectorContext(
            key=reg.key, decl=reg.decl, run_id=run_id, dry_run=dry_run, secrets=self.secrets
        )
        info = RunInfo(connector=reg.key, run_id=run_id, dry_run=dry_run, owns=reg.owns)
        status = "succeeded"
        token = auth.set_current_token(reg.principal)
        saver = asyncio.create_task(self._save_progress_every(ctx, run_id))
        try:
            with _in_run(info):
                await reg.fn(ctx)
                if not dry_run and reg.decl.missing == "flag":
                    async with self._maker()() as s:
                        for source_type in sorted(ctx._complete):  # pyright: ignore[reportPrivateUsage]
                            n = await self.refs.mark_missing(
                                s, connector=reg.key, source_type=source_type, run_id=run_id
                            )
                            if n:
                                ctx.count("missing from source", n)
                                ctx.problem(
                                    f"{n} record(s) are no longer in {reg.decl.system}; "
                                    "they're flagged on their pages for review.",
                                    item=source_type,
                                )
                        await s.commit()
            if ctx.problems:
                status = "problems"
        except Exception as exc:
            status = "failed"
            log.exception("connector %s run %s failed", reg.key, run_id)
            ctx.problem(f"The run stopped: {exc}")
        finally:
            saver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await saver
            auth.reset_current(token)
            async with self._maker()() as s:
                await s.execute(
                    text(
                        "UPDATE itops.connector_runs SET finished_at = sysutcdatetime(),"
                        " status = :st, counts = :c, problems = :p, summary = :sm,"
                        " progress = :pr WHERE id = :i"
                    ),
                    {
                        "st": status, "c": json.dumps(ctx.counts),
                        "p": json.dumps(ctx.problems) if ctx.problems else None,
                        "sm": summarize(ctx.counts)[:500],
                        "pr": json.dumps(ctx.progress) if ctx.progress else None, "i": run_id,
                    },
                )  # fmt: skip
                await s.commit()

    async def _save_progress_every(self, ctx: ConnectorContext, run_id: int) -> None:
        """While a run runs: save its step, counts and problems when they moved, so its page
        can show progress (and another worker process can too)."""
        saved = 0
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            if ctx.changes == saved:
                continue
            saved = ctx.changes
            try:
                async with self._maker()() as s:
                    await s.execute(
                        text(
                            "UPDATE itops.connector_runs SET counts = :c, problems = :p,"
                            " summary = :sm, progress = :pr WHERE id = :i AND status = 'running'"
                        ),
                        {
                            "c": json.dumps(ctx.counts),
                            "p": json.dumps(ctx.problems) if ctx.problems else None,
                            "sm": summarize(ctx.counts)[:500],
                            "pr": json.dumps(ctx.progress) if ctx.progress else None,
                            "i": run_id,
                        },
                    )
                    await s.commit()
            except Exception:  # progress is a nicety: never let it break the run
                log.warning("couldn't save progress of run %s", run_id, exc_info=True)

    # -- scheduling

    async def recover(self) -> int:
        """At startup: runs left 'running' by a stopped process are marked failed."""
        async with self._maker()() as s:
            result = await s.execute(
                text(
                    "UPDATE itops.connector_runs SET status = 'failed',"
                    " finished_at = sysutcdatetime(), summary = 'Interrupted (the service stopped)'"
                    " WHERE status = 'running'"
                )
            )
            await s.commit()
            return _rowcount(result)

    async def tick(self, now: dt.datetime | None = None) -> list[str]:
        """Start every enabled connector whose schedule is due; returns their keys."""
        now = now or utcnow()
        enabled = await self.enabled()
        started: list[str] = []
        for reg in self.all():
            if (
                not enabled.get(reg.key)
                or reg.decl.schedule == "manual"
                or self.is_running(reg.key)
            ):
                continue
            if is_due(reg.decl.schedule, await self.last_scheduled_start(reg.key), now):
                await self.start(reg.key, trigger="schedule")
                started.append(reg.key)
        return started

    async def scheduler(self) -> None:
        """Runs for the life of the service (started in the app's lifespan)."""
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("connector scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)
