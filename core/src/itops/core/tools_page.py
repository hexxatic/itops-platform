"""``/tools``: every tool action, under one Tools header (ADR 0011).

The list groups actions by provider; each action has a page with its inputs, a Run
button and its history; each run has a page with live progress (steps, a progress bar,
counts, problems and the log) that refreshes itself until the run ends. AI run requests
wait on the list for someone allowed to run the tool.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.actions import ActionBusy, ActionError, ActionRun, RegisteredAction
from itops.core.runtime import Runtime

STATUS_TONE = {
    "running": "info", "succeeded": "success", "problems": "warning", "failed": "danger",
    "cancelled": "neutral",
}  # fmt: skip
STATUS_TEXT = {
    "running": "Running", "succeeded": "Succeeded", "problems": "Finished with problems",
    "failed": "Failed", "cancelled": "Cancelled",
}  # fmt: skip
DANGER_TONE = {"safe": "success", "caution": "warning", "dangerous": "danger"}
INPUT_PREFIX = "in__"
LOG_TAIL = 40


def _when(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _status(run: ActionRun | None) -> dict[str, str] | str:
    if run is None:
        return "Never run"
    return {"badge": STATUS_TEXT.get(run.status, run.status),
            "tone": STATUS_TONE.get(run.status, "neutral")}  # fmt: skip


def _inputs_text(inputs: dict[str, Any], action: RegisteredAction | None) -> str:
    labels = {i.key: i.label for i in action.decl.inputs} if action else {}
    return ", ".join(f"{labels.get(k, k)}: {v}" for k, v in inputs.items())


def mount_tools(app: FastAPI, templates: Jinja2Templates, runtime: Runtime) -> None:
    runner = runtime.actions

    def signed_in() -> auth.Principal:
        who = auth.current()
        if who is None:
            raise HTTPException(status_code=401)
        return who

    def action_or_404(key: str) -> RegisteredAction:
        action = runner.get(key)
        if action is None:
            raise HTTPException(status_code=404, detail="No such tool")
        return action

    def may_see(action: RegisteredAction) -> None:
        who = signed_in()
        if not who.holds(action.decl.permission):
            raise HTTPException(status_code=403, detail=f"You can't run {action.decl.title}.")

    def back(url: str, error: str | None = None, notice: str | None = None) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        if error:
            url += f"{sep}error={quote(error)}"
        elif notice:
            url += f"{sep}notice={quote(notice)}"
        return RedirectResponse(url, status_code=303)

    def run_view(run: ActionRun) -> dict[str, Any]:
        action = runner.get(run.action)
        lines = run.log.splitlines()
        return {
            "run": run,
            "title": action.decl.title if action else run.action,
            "status": _status(run),
            "counts": [{"outcome": k, "count": n} for k, n in run.counts.items()],
            "log_tail": "\n".join(lines[-LOG_TAIL:]),
            "log_lines": len(lines),
            "inputs": _inputs_text(run.inputs, action),
        }

    @app.get("/tools", response_class=HTMLResponse, include_in_schema=False)
    async def tools_page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        who = signed_in()
        visible = runner.visible(who)
        keys = {a.key for a in visible}
        last = await runner.last_runs()
        groups: dict[str, list[dict[str, Any]]] = {}
        for a in visible:
            run = last.get(a.key)
            groups.setdefault(a.module_title, []).append(
                {
                    "action": a,
                    "last": run,
                    "status": _status(run),
                    "danger_tone": DANGER_TONE[a.decl.danger],
                }
            )
        requests = [
            {
                "req": r,
                "title": runner.get(r.action).decl.title,  # type: ignore[union-attr]
                "inputs": _inputs_text(r.inputs, runner.get(r.action)),
            }
            for r in await runner.requests()
            if r.action in keys
        ]
        recent = [r for r in await runner.runs(limit=40) if r.action in keys][:10]
        return templates.TemplateResponse(
            request,
            "core/tools.html",
            {
                "page_title": "Tools",
                "current_module": "tools",
                "groups": groups,
                "requests": requests,
                "can_decide": not who.via_ai,
                "recent": [
                    {
                        "started": {"text": _when(r.started_at), "href": f"/tools/runs/{r.id}"},
                        "tool": runner.get(r.action).decl.title  # type: ignore[union-attr]
                        if runner.get(r.action)
                        else r.action,
                        "status": _status(r),
                        "by": r.started_by_name,
                        "summary": r.summary,
                        "took": r.duration,
                    }
                    for r in recent
                ],
                "error": request.query_params.get("error"),
                "notice": request.query_params.get("notice"),
            },
        )

    @app.get("/tools/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
    async def run_page(request: Request, run_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        run = await runner.run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="No such run")
        action = runner.get(run.action)
        if action is not None:
            may_see(action)
        return templates.TemplateResponse(
            request,
            "core/tool_run.html",
            {
                "page_title": f"Run {run.id}",
                "current_module": "tools",
                **run_view(run),
                "action": action,
                "can_cancel": action is not None and not signed_in().via_ai,
                "started": _when(run.started_at),
                "finished": _when(run.finished_at),
                "error": request.query_params.get("error"),
                "notice": request.query_params.get("notice"),
            },
        )

    @app.get("/tools/runs/{run_id}/progress", response_class=HTMLResponse, include_in_schema=False)
    async def run_progress(request: Request, run_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        """The live part of a running run's page (htmx polls it); once it has ended, the
        page reloads to show the outcome."""
        run = await runner.run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="No such run")
        if (action := runner.get(run.action)) is not None:
            may_see(action)
        if run.status != "running":
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return templates.TemplateResponse(request, "core/tool_progress.html", run_view(run))

    @app.post("/tools/runs/{run_id}/cancel", include_in_schema=False)
    async def cancel(run_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            await runner.cancel(run_id)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ActionError as exc:
            return back(f"/tools/runs/{run_id}", str(exc))
        return back(f"/tools/runs/{run_id}", notice="Cancelling: it stops at its next step.")

    @app.post("/tools/requests/{request_id}/{decision}", include_in_schema=False)
    async def decide(request_id: int, decision: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if decision not in ("approve", "decline"):
            raise HTTPException(status_code=404)
        try:
            run_id = await runner.decide(request_id, approve=decision == "approve")
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ActionBusy as exc:
            if exc.run is not None:
                return back(f"/tools/runs/{exc.run.id}", str(exc))
            return back("/tools", str(exc))
        except ActionError as exc:
            return back("/tools", str(exc))
        if run_id is not None:
            return back(f"/tools/runs/{run_id}")
        return back("/tools", notice="Request declined.")

    # Registered last: "/tools/{key}" would otherwise match "/tools/runs/...".
    @app.get("/tools/{key}", response_class=HTMLResponse, include_in_schema=False)
    async def tool_page(request: Request, key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        action = action_or_404(key)
        may_see(action)
        who = signed_in()
        running = await runner.running(key)
        runs = await runner.runs(key, limit=20)
        return templates.TemplateResponse(
            request,
            "core/tool.html",
            {
                "page_title": action.decl.title,
                "current_module": "tools",
                "action": action,
                "danger_tone": DANGER_TONE[action.decl.danger],
                "running": running,
                "can_run": not who.via_ai,
                "input_prefix": INPUT_PREFIX,
                "fields": [
                    {"spec": i, "options": [(o, o) for o in i.options]} for i in action.decl.inputs
                ],
                "confirm": action.decl.confirm
                or (f"Run {action.decl.title} now?" if action.decl.danger != "safe" else ""),
                "history": [
                    {
                        "started": {"text": _when(r.started_at), "href": f"/tools/runs/{r.id}"},
                        "status": _status(r),
                        "by": r.started_by_name,
                        "inputs": _inputs_text(r.inputs, action),
                        "summary": r.summary,
                        "took": r.duration,
                    }
                    for r in runs
                ],
                "error": request.query_params.get("error"),
            },
        )

    @app.post("/tools/{key}/run", include_in_schema=False)
    async def start(request: Request, key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        action = action_or_404(key)
        form = await request.form()
        inputs = {
            k.removeprefix(INPUT_PREFIX): str(v)
            for k, v in form.items()
            if k.startswith(INPUT_PREFIX)
        }
        try:
            run_id = await runner.start(action.key, inputs)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ActionBusy as exc:
            # Someone else is running it: send them to watch that run.
            if exc.run is not None:
                return back(f"/tools/runs/{exc.run.id}", str(exc))
            return back(f"/tools/{key}", str(exc))
        except ActionError as exc:
            return back(f"/tools/{key}", str(exc))
        return back(f"/tools/runs/{run_id}")
