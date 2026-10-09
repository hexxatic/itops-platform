"""Tool actions (ADR 0011): fixed operations people run from ``/tools``.

A provider module registers ``@module.action("sync_ad")`` (``async def run(ctx)``) and
declares it in its manifest under ``[actions.<name>]``: title, description, the
human-only permission that runs it, typed inputs, a confirmation, a danger level and a
timeout. The core lists every action under one Tools header and runs it in the service:

* **One run at a time per action**, whichever worker starts it (a unique index on running
  rows). Someone starting it again is told who is running it and since when.
* **Visible progress**: the run reports steps, counts, problems and log lines through its
  context; they're saved every couple of seconds and the run page refreshes itself.
* **People run, AI requests.** Starting a run needs the action's human-only permission in
  the web UI. AI tools (and tokens) may *request* a run; someone allowed to run it approves
  (it starts) or declines on the Tools page.
* **The remote endpoint is the boundary.** Actions reach other servers through constrained
  PowerShell endpoints (JEA) with ``ctx.powershell(...)``: one named command, no arguments,
  as IT-Ops' own service account. The endpoint decides what that command may do.
* Every request, run and outcome is in the audit trail. A run that outlives its timeout is
  stopped; a run can be cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core import auth
from itops.core.audit import AuditLog
from itops.core.connectors import SecretReader, read_secret, summarize, utcnow
from itops.core.manifest import ActionDecl

log = logging.getLogger("itops.actions")

PROGRESS_SECONDS = 2.0  # how often a running action's progress is saved
MAX_LOG_LINES = 2000
MAX_LOG_CHARS = 200_000
MAX_PROBLEMS = 200
MAX_TEXT_INPUT = 200
_COMPUTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")
_ENDPOINT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,62}$")
_COMMAND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]{0,62}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ActionError(Exception):
    """A problem a person should read (bad input, a busy action, a failed step)."""


@dataclass(frozen=True)
class ActionRun:
    id: int
    action: str
    started_by: str
    started_by_name: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    status: str  # running, succeeded, problems, failed, cancelled
    inputs: dict[str, Any]
    progress: dict[str, Any]
    counts: dict[str, int]
    problems: list[dict[str, str]]
    log: str
    summary: str
    request_id: int | None
    cancel_requested: bool

    @property
    def duration(self) -> str:
        """How long it took; while it runs, how long it has been running."""
        end = self.finished_at or (utcnow() if self.status == "running" else None)
        if end is None:
            return ""
        secs = max(0, int((end - self.started_at).total_seconds()))
        return f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"


class ActionBusy(ActionError):
    """The action is already running: says who started it and when."""

    def __init__(self, title: str, run: ActionRun | None) -> None:
        self.run = run
        if run is None:
            super().__init__(f"{title} is already running.")
            return
        super().__init__(
            f"{title} is already running: started by {run.started_by_name} at "
            f"{run.started_at:%H:%M} UTC ({run.duration} ago)."
        )


@dataclass(frozen=True)
class ActionRequest:
    id: int
    action: str
    inputs: dict[str, Any]
    reason: str
    requested_by: str
    requested_by_name: str
    requested_at: dt.datetime
    via_ai: bool
    status: str  # pending, approved, declined
    decided_by: str | None
    decided_at: dt.datetime | None
    run_id: int | None


_RUN_COLUMNS = (
    "id, action, started_by, started_by_name, started_at, finished_at, status, inputs, progress,"
    " counts, problems, log, summary, request_id, cancel_requested"
)
_REQUEST_COLUMNS = (
    "id, action, inputs, reason, requested_by, requested_by_name, requested_at, via_ai, status,"
    " decided_by, decided_at, run_id"
)


def _json(value: Any, default: Any) -> Any:
    return json.loads(value) if value else default


def _run_row(r: Any) -> ActionRun:
    return ActionRun(
        id=r[0],
        action=r[1],
        started_by=r[2],
        started_by_name=r[3],
        started_at=r[4],
        finished_at=r[5],
        status=r[6],
        inputs=_json(r[7], {}),
        progress=_json(r[8], {}),
        counts=_json(r[9], {}),
        problems=_json(r[10], []),
        log=r[11] or "",
        summary=r[12] or "",
        request_id=r[13],
        cancel_requested=bool(r[14]),
    )


def _request_row(r: Any) -> ActionRequest:
    return ActionRequest(
        id=r[0],
        action=r[1],
        inputs=_json(r[2], {}),
        reason=r[3] or "",
        requested_by=r[4],
        requested_by_name=r[5],
        requested_at=r[6],
        via_ai=bool(r[7]),
        status=r[8],
        decided_by=r[9],
        decided_at=r[10],
        run_id=r[11],
    )


Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]


class ActionContext:
    """Handed to an action's run function: its inputs, and how it reports progress."""

    # How remote commands are started (tests swap in a fake).
    spawn: Spawn = staticmethod(asyncio.create_subprocess_exec)  # type: ignore[assignment]

    def __init__(
        self,
        *,
        key: str,
        decl: ActionDecl,
        run_id: int,
        inputs: dict[str, Any],
        secrets: SecretReader,
    ) -> None:
        self.key = key
        self.title = decl.title
        self.run_id = run_id
        self.inputs = inputs
        self.counts: dict[str, int] = {}
        self.problems: list[dict[str, str]] = []
        self.progress: dict[str, Any] = {}
        self.lines: list[str] = []
        self.changes = 0
        self._decl = decl
        self._secrets = secrets
        self._chars = 0

    def step(self, name: str, *, total: int | None = None) -> None:
        """Start the next step ("Replicating domain controllers"), shown live on the run
        page. ``total``: how many items it will process, when known (a progress bar)."""
        number = int(self.progress.get("number", 0)) + 1
        self.progress = {"step": name[:100], "number": number, "done": 0, "total": total}
        self.log(f"== Step {number}: {name}")

    def advance(self, n: int = 1) -> None:
        if self.progress:
            self.progress["done"] = int(self.progress["done"]) + n
        self.changes += 1

    def count(self, outcome: str, n: int = 1) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + n
        self.changes += 1

    def problem(self, message: str, *, item: str | None = None) -> None:
        """Something a person should look at (the run carries on)."""
        self.changes += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append({"item": (item or "")[:300], "text": message[:1000]})

    def log(self, line: str) -> None:
        """A line of output for the run page (kept to a sensible size; never put secrets
        here)."""
        for part in str(line).splitlines() or [""]:
            if len(self.lines) >= MAX_LOG_LINES or self._chars >= MAX_LOG_CHARS:
                if not self.lines or self.lines[-1] != "… (output trimmed)":
                    self.lines.append("… (output trimmed)")
                return
            part = part.rstrip()[:1000]
            self.lines.append(part)
            self._chars += len(part) + 1
        self.changes += 1

    def secret(self, name: str) -> str:
        """A declared secret, from ``ITOPS_<MODULE>_<NAME>`` (never stored in IT-Ops)."""
        if name not in self._decl.secrets:
            raise ActionError(f"secret {name!r} isn't declared in the action's manifest")
        env_name = f"ITOPS_{self.key.split('.', 1)[0]}_{name}".upper()
        value = self._secrets(env_name)
        if not value:
            raise ActionError(f"{env_name} isn't set on this server")
        return value

    async def powershell(self, computer: str, endpoint: str, command: str) -> str:
        """Run ``command`` (a function the endpoint exposes; no arguments) on ``computer``
        through the constrained endpoint ``endpoint``, as IT-Ops' own service account.
        Output lines go to the run log as they arrive; a failure raises ActionError."""
        if not _COMPUTER_RE.match(computer):
            raise ActionError(f"not a computer name: {computer!r}")
        if not _ENDPOINT_RE.match(endpoint):
            raise ActionError(f"not an endpoint name: {endpoint!r}")
        if not _COMMAND_RE.match(command):
            raise ActionError(f"not a command name: {command!r}")
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"Invoke-Command -ComputerName '{computer}' -ConfigurationName '{endpoint}' "
            f"-ScriptBlock ([scriptblock]::Create('{command}')) | Out-String -Stream -Width 200"
        )
        self.log(f"> {command} on {computer} ({endpoint})")
        proc = await type(self).spawn(
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out: list[str] = []
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    out.append(line)
                    self.log(line)
            err = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
            code = await proc.wait()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise
        if code != 0:
            raise ActionError(_remote_failure(command, computer, endpoint, err, code))
        return "\n".join(out)


_PS_NOISE_RE = re.compile(
    r"^.*?failed with the following error message\s*:\s*|\s+For more information, see the "
    r"about_\w+ Help topic\..*$|\s+At line:\d+ char:\d+.*$",
    re.DOTALL,
)


def _remote_failure(command: str, computer: str, endpoint: str, err: str, code: int) -> str:
    """A failed remote call as a sentence a person can act on (not a PowerShell dump)."""
    text = " ".join(err.split())
    host = computer.split(".", 1)[0]
    if "Access is denied" in text or "AccessDenied" in text:
        return (
            f"{host} refused this IT-Ops server's account on the {endpoint} endpoint (access "
            "denied). Each endpoint allows only the production IT-Ops account, so this is "
            "expected on test; on production, check the endpoint is installed for it."
        )
    if "cannot be resolved" in text or "WinRM cannot complete" in text:
        return f"Couldn't reach {host} over PowerShell remoting: is it up and reachable?"
    if "not recognized as the name of a cmdlet" in text:
        return f"{endpoint} on {host} doesn't offer {command}: is the endpoint up to date?"
    detail = _PS_NOISE_RE.sub("", text).strip() or f"exit code {code}"
    return f"{command} on {host} failed: {detail[:400]}"


ActionFn = Callable[[ActionContext], Awaitable[None]]


@dataclass
class RegisteredAction:
    key: str  # "<module>.<name>"
    module: str
    module_title: str
    name: str
    decl: ActionDecl
    fn: ActionFn


def _no_tasks() -> dict[int, asyncio.Task[None]]:
    return {}


def _require_human_with(permission: str) -> auth.Principal:
    """The action's permission, held by a person in the web UI (never via AI)."""
    return auth.require(permission)  # human-only permissions refuse AI connections


@dataclass
class ActionRunner:
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    audit: AuditLog = field(default_factory=AuditLog)
    secrets: SecretReader = read_secret
    _actions: dict[str, RegisteredAction] = field(default_factory=dict[str, RegisteredAction])
    _tasks: dict[int, asyncio.Task[None]] = field(default_factory=lambda: _no_tasks())

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        assert self.sessionmaker is not None, "tool actions need a database"
        return self.sessionmaker

    # -- registry

    def register(
        self, module: str, module_title: str, name: str, decl: ActionDecl, fn: ActionFn
    ) -> None:
        key = f"{module}.{name}"
        self._actions[key] = RegisteredAction(key, module, module_title, name, decl, fn)

    def all(self) -> list[RegisteredAction]:
        return sorted(self._actions.values(), key=lambda a: (a.module_title, a.decl.title))

    def get(self, key: str) -> RegisteredAction | None:
        return self._actions.get(key)

    def visible(self, principal: auth.Principal | None) -> list[RegisteredAction]:
        """The actions ``principal`` may run (or, via AI, request)."""
        return [a for a in self.all() if principal and principal.holds(a.decl.permission)]

    def validate_inputs(self, action: RegisteredAction, raw: dict[str, Any]) -> dict[str, Any]:
        """Typed, checked inputs; unknown or bad ones are refused before anything runs."""
        declared = {i.key: i for i in action.decl.inputs}
        unknown = sorted(set(raw) - set(declared))
        if unknown:
            raise ActionError(f"unknown input(s): {', '.join(unknown)}")
        out: dict[str, Any] = {}
        for key, spec in declared.items():
            value = str(raw.get(key, spec.default) or "").strip()
            if not value:
                if spec.required:
                    raise ActionError(f"{spec.label} is required.")
                continue
            if spec.kind == "select" and value not in spec.options:
                raise ActionError(f"{spec.label}: choose one of {', '.join(spec.options)}.")
            if spec.kind == "number":
                try:
                    out[key] = float(value) if "." in value else int(value)
                except ValueError as exc:
                    raise ActionError(f"{spec.label} must be a number.") from exc
                continue
            if spec.kind == "text" and (len(value) > MAX_TEXT_INPUT or _CONTROL_RE.search(value)):
                raise ActionError(f"{spec.label}: up to {MAX_TEXT_INPUT} plain characters.")
            out[key] = value
        return out

    # -- runs

    async def runs(self, key: str | None = None, *, limit: int = 30) -> list[ActionRun]:
        where = "WHERE action = :k" if key else ""
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    f"SELECT TOP (:n) {_RUN_COLUMNS} FROM itops.action_runs {where}"
                    " ORDER BY id DESC"
                ),
                {"n": limit, "k": key},
            )
            return [_run_row(r) for r in rows.all()]

    async def run(self, run_id: int) -> ActionRun | None:
        async with self._maker()() as s:
            row = (
                await s.execute(
                    text(f"SELECT {_RUN_COLUMNS} FROM itops.action_runs WHERE id = :i"),
                    {"i": run_id},
                )
            ).first()
            return _run_row(row) if row else None

    async def running(self, key: str) -> ActionRun | None:
        async with self._maker()() as s:
            row = (
                await s.execute(
                    text(
                        f"SELECT {_RUN_COLUMNS} FROM itops.action_runs"
                        " WHERE action = :k AND status = 'running'"
                    ),
                    {"k": key},
                )
            ).first()
            return _run_row(row) if row else None

    async def last_runs(self) -> dict[str, ActionRun]:
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    f"SELECT {_RUN_COLUMNS} FROM itops.action_runs WHERE id IN"
                    " (SELECT MAX(id) FROM itops.action_runs GROUP BY action)"
                )
            )
            return {r.action: r for r in (_run_row(x) for x in rows.all())}

    async def start(
        self, key: str, raw_inputs: dict[str, Any] | None = None, *, request_id: int | None = None
    ) -> int:
        """Start a run in the background; returns its id. A person with the action's
        permission, in the web UI. Raises ActionBusy (who and when) if it's running."""
        action = self._actions.get(key)
        if action is None:
            raise ActionError(f"no tool {key!r}")
        who = _require_human_with(action.decl.permission)
        inputs = self.validate_inputs(action, raw_inputs or {})
        async with self._maker()() as s:
            try:
                run_id = int(
                    (
                        await s.execute(
                            text(
                                "INSERT INTO itops.action_runs (action, started_by,"
                                " started_by_name, status, inputs, request_id) OUTPUT inserted.id"
                                " VALUES (:k, :u, :n, 'running', :i, :r)"
                            ),
                            {
                                "k": key,
                                "u": who.user,
                                "n": who.display_name,
                                "i": json.dumps(inputs),
                                "r": request_id,
                            },
                        )
                    ).scalar_one()
                )
            except IntegrityError:
                await s.rollback()
                raise ActionBusy(action.decl.title, await self.running(key)) from None
            await self.audit.record(
                s,
                module="itops",
                action="tool.run_started",
                entity_kind="tool",
                entity_id=key,
                title=action.decl.title,
                summary=f"Started {action.decl.title}",
                details={
                    "run": run_id,
                    "inputs": inputs,
                    **({"request": request_id} if request_id else {}),
                },
            )
            await s.commit()
        self._tasks[run_id] = asyncio.create_task(self._execute(action, run_id, inputs, who))
        return run_id

    async def wait(self, run_id: int) -> None:
        """Wait for a run started in this process to finish (tests, shutdown)."""
        task = self._tasks.get(run_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def cancel(self, run_id: int) -> None:
        run = await self.run(run_id)
        action = self._actions.get(run.action) if run else None
        if run is None or action is None:
            raise ActionError("no such run")
        _require_human_with(action.decl.permission)
        if run.status != "running":
            raise ActionError("That run has already finished.")
        async with self._maker()() as s:
            await s.execute(
                text("UPDATE itops.action_runs SET cancel_requested = 1 WHERE id = :i"),
                {"i": run_id},
            )
            await s.commit()
        if (task := self._tasks.get(run_id)) is not None:
            task.cancel()  # another worker's run stops at its next progress save

    async def recover(self) -> int:
        """At startup: runs left 'running' by a stopped process are marked failed."""
        async with self._maker()() as s:
            result = await s.execute(
                text(
                    "UPDATE itops.action_runs SET status = 'failed',"
                    " finished_at = sysutcdatetime(),"
                    " summary = 'Interrupted (the service stopped)' WHERE status = 'running'"
                )
            )
            await s.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def _execute(
        self,
        action: RegisteredAction,
        run_id: int,
        inputs: dict[str, Any],
        who: auth.Principal,
    ) -> None:
        ctx = ActionContext(
            key=action.key, decl=action.decl, run_id=run_id, inputs=inputs, secrets=self.secrets
        )
        status = "succeeded"
        token = auth.set_current_token(who)
        saver = asyncio.create_task(self._save_progress_every(ctx, run_id))
        try:
            async with asyncio.timeout(action.decl.timeout * 60):
                await action.fn(ctx)
            if ctx.problems:
                status = "problems"
        except asyncio.CancelledError:
            status = "cancelled"
            ctx.log("Cancelled.")
        except TimeoutError:
            status = "failed"
            ctx.problem(f"Stopped: it ran longer than its {action.decl.timeout}-minute timeout.")
        except ActionError as exc:
            status = "failed"
            ctx.problem(str(exc))
        except Exception as exc:
            status = "failed"
            log.exception("tool %s run %s failed", action.key, run_id)
            ctx.problem(f"The run stopped: {type(exc).__name__}: {exc}")
        finally:
            saver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await saver
            auth.reset_current(token)
            await self._finish(action, run_id, ctx, status)
            self._tasks.pop(run_id, None)

    def _snapshot(self, ctx: ActionContext) -> dict[str, Any]:
        return {
            "c": json.dumps(ctx.counts),
            "p": json.dumps(ctx.problems) if ctx.problems else None,
            "pr": json.dumps(ctx.progress) if ctx.progress else None,
            "l": "\n".join(ctx.lines),
        }

    async def _finish(
        self, action: RegisteredAction, run_id: int, ctx: ActionContext, status: str
    ) -> None:
        summary = (
            summarize(ctx.counts)
            if ctx.counts
            else {
                "succeeded": "Done",
                "problems": f"Done with {len(ctx.problems)} problem(s)",
                "failed": "Failed",
                "cancelled": "Cancelled",
            }.get(status, status)
        )
        async with self._maker()() as s:
            await s.execute(
                text(
                    "UPDATE itops.action_runs SET finished_at = sysutcdatetime(), status = :st,"
                    " counts = :c, problems = :p, progress = :pr, log = :l, summary = :sm"
                    " WHERE id = :i"
                ),
                {**self._snapshot(ctx), "st": status, "sm": summary[:500], "i": run_id},
            )
            await self.audit.record(
                s,
                module="itops",
                action="tool.run_finished",
                entity_kind="tool",
                entity_id=action.key,
                title=action.decl.title,
                summary=f"{action.decl.title}: {summary}",
                details={"run": run_id, "status": status},
            )
            await s.commit()

    async def _save_progress_every(self, ctx: ActionContext, run_id: int) -> None:
        """While a run runs: save its progress when it moved, so its page shows it (from
        any worker), and stop it if someone asked another worker to cancel it."""
        saved = -1
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            try:
                async with self._maker()() as s:
                    if ctx.changes != saved:
                        saved = ctx.changes
                        await s.execute(
                            text(
                                "UPDATE itops.action_runs SET counts = :c, problems = :p,"
                                " progress = :pr, log = :l WHERE id = :i AND status = 'running'"
                            ),
                            {**self._snapshot(ctx), "i": run_id},
                        )
                        await s.commit()
                    cancel = (
                        await s.execute(
                            text("SELECT cancel_requested FROM itops.action_runs WHERE id = :i"),
                            {"i": run_id},
                        )
                    ).scalar()
            except Exception:  # progress is a nicety: never let it break the run
                log.warning("couldn't save progress of tool run %s", run_id, exc_info=True)
                continue
            if cancel and (task := self._tasks.get(run_id)) is not None:
                task.cancel()
                return

    # -- requests (AI asks, a person decides)

    async def request(self, key: str, raw_inputs: dict[str, Any] | None, reason: str) -> int:
        """Ask for a run (AI tools and tokens): someone allowed to run it decides. The
        asker's role must allow running it."""
        action = self._actions.get(key)
        if action is None:
            raise ActionError(f"no tool {key!r}")
        who = auth.current()
        if who is None or not who.holds(action.decl.permission):
            raise auth.PermissionDenied(f"you may not run or request {action.decl.title}")
        inputs = self.validate_inputs(action, raw_inputs or {})
        async with self._maker()() as s:
            request_id = int(
                (
                    await s.execute(
                        text(
                            "INSERT INTO itops.action_requests (action, inputs, reason,"
                            " requested_by, requested_by_name, via_ai, status)"
                            " OUTPUT inserted.id VALUES (:k, :i, :r, :u, :n, :ai, 'pending')"
                        ),
                        {
                            "k": key,
                            "i": json.dumps(inputs),
                            "r": reason.strip()[:1000],
                            "u": who.user,
                            "n": who.display_name,
                            "ai": who.via_ai,
                        },
                    )
                ).scalar_one()
            )
            await self.audit.record(
                s,
                module="itops",
                action="tool.run_requested",
                entity_kind="tool",
                entity_id=key,
                title=action.decl.title,
                summary=f"Requested a run of {action.decl.title}",
                details={"request": request_id, "inputs": inputs, "reason": reason[:300]},
            )
            await s.commit()
        return request_id

    async def requests(self, *, pending_only: bool = True, limit: int = 50) -> list[ActionRequest]:
        where = "WHERE status = 'pending'" if pending_only else ""
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    f"SELECT TOP (:n) {_REQUEST_COLUMNS} FROM itops.action_requests {where}"
                    " ORDER BY id DESC"
                ),
                {"n": limit},
            )
            return [_request_row(r) for r in rows.all()]

    async def get_request(self, request_id: int) -> ActionRequest | None:
        async with self._maker()() as s:
            row = (
                await s.execute(
                    text(f"SELECT {_REQUEST_COLUMNS} FROM itops.action_requests WHERE id = :i"),
                    {"i": request_id},
                )
            ).first()
            return _request_row(row) if row else None

    async def decide(self, request_id: int, *, approve: bool) -> int | None:
        """Approve (the run starts, as the approver) or decline a pending request."""
        req = await self.get_request(request_id)
        action = self._actions.get(req.action) if req else None
        if req is None or action is None:
            raise ActionError("no such request")
        who = _require_human_with(action.decl.permission)
        if req.status != "pending":
            raise ActionError("That request has already been decided.")
        run_id = await self.start(req.action, req.inputs, request_id=req.id) if approve else None
        async with self._maker()() as s:
            await s.execute(
                text(
                    "UPDATE itops.action_requests SET status = :st, decided_by = :u,"
                    " decided_at = sysutcdatetime(), run_id = :r WHERE id = :i"
                ),
                {
                    "st": "approved" if approve else "declined",
                    "u": who.user,
                    "r": run_id,
                    "i": req.id,
                },
            )
            if not approve:
                await self.audit.record(
                    s,
                    module="itops",
                    action="tool.request_declined",
                    entity_kind="tool",
                    entity_id=req.action,
                    title=action.decl.title,
                    summary=f"Declined a request to run {action.decl.title}",
                    details={"request": req.id},
                )
            await s.commit()
        return run_id
