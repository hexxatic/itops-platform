"""``/connectors``: every data flow in and out of IT-Ops, for admins (ADR 0010).

List connectors with their schedule and last run, enable or disable them, start a run or
a dry run, and read each run's counts and problems.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.connectors import (
    CONNECTORS_PERMISSION,
    HUMAN_ONLY,
    ConnectorError,
    Registered,
    Run,
    next_due,
    utcnow,
)
from itops.core.runtime import Runtime

STATUS_TONE = {"running": "info", "succeeded": "success", "problems": "warning", "failed": "danger"}
STATUS_TEXT = {
    "running": "Running",
    "succeeded": "Succeeded",
    "problems": "Finished with problems",
    "failed": "Failed",
}


def _when(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _status(run: Run | None) -> dict[str, str] | str:
    if run is None:
        return "Never run"
    label = STATUS_TEXT.get(run.status, run.status) + (" (dry run)" if run.dry_run else "")
    return {"badge": label, "tone": STATUS_TONE.get(run.status, "neutral")}


def _schedule(reg: Registered) -> str:
    s = reg.decl.schedule
    return (
        "Manual only"
        if s == "manual"
        else s.replace("every ", "Every ").replace("daily ", "Daily at ")
    )


def mount_connectors(app: FastAPI, templates: Jinja2Templates, runtime: Runtime) -> None:
    runner = runtime.connectors

    def require() -> auth.Principal:
        try:
            return auth.require(CONNECTORS_PERMISSION)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Connectors are for admins.") from exc

    def require_human() -> None:
        p = auth.current()
        if p is not None and p.via_ai:
            raise HTTPException(status_code=403, detail=HUMAN_ONLY)

    def connector_or_404(key: str) -> Registered:
        reg = runner.get(key)
        if reg is None:
            raise HTTPException(status_code=404, detail="No such connector")
        return reg

    def back(url: str, error: str | None = None) -> RedirectResponse:
        if error:
            url += ("&" if "?" in url else "?") + "error=" + quote(error)
        return RedirectResponse(url, status_code=303)

    @app.get("/connectors", response_class=HTMLResponse, include_in_schema=False)
    async def connectors_page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        require()
        enabled = await runner.enabled()
        last = await runner.last_runs()
        now = utcnow()
        rows: list[dict[str, Any]] = []
        for reg in runner.all():
            is_on = enabled.get(reg.key, False)
            due = None
            if is_on:
                due = next_due(reg.decl.schedule, await runner.last_scheduled_start(reg.key), now)
            rows.append(
                {
                    "name": {"text": reg.decl.title, "href": f"/connectors/{reg.key}"},
                    "system": reg.decl.system,
                    "direction": "Into IT-Ops" if reg.decl.direction == "in" else "Out of IT-Ops",
                    "schedule": _schedule(reg),
                    "state": {"badge": "On", "tone": "success"}
                    if is_on
                    else {"badge": "Off", "tone": "neutral"},
                    "last": _status(last.get(reg.key)),
                    "when": _when(last[reg.key].started_at) if reg.key in last else "",
                    "next": "Now" if due and due <= now else _when(due),
                }
            )
        return templates.TemplateResponse(
            request, "core/connectors.html", {"page_title": "Connectors", "rows": rows}
        )

    @app.get("/connectors/{key}", response_class=HTMLResponse, include_in_schema=False)
    async def connector_page(request: Request, key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        require()
        reg = connector_or_404(key)
        runs = await runner.runs(key)
        owns = [
            {"kind": kind, "fields": "every field" if "*" in keys else ", ".join(keys)}
            for kind, keys in reg.decl.owns.items()
        ]
        return templates.TemplateResponse(
            request,
            "core/connector.html",
            {
                "page_title": reg.decl.title,
                "reg": reg,
                "enabled": (await runner.enabled()).get(key, False),
                "running": runner.is_running(key),
                "running_run": next((r.id for r in runs if r.status == "running"), None),
                "schedule": _schedule(reg),
                "owns": owns,
                "runs": [
                    {
                        "started": {
                            "text": _when(r.started_at),
                            "href": f"/connectors/runs/{r.id}",
                        },
                        "status": _status(r),
                        "trigger": "Scheduled" if r.trigger == "schedule" else f"By {r.started_by}",
                        "summary": r.summary,
                        "problems": len(r.problems) or "",
                        "duration": r.duration,
                    }
                    for r in runs
                ],
                "error": request.query_params.get("error"),
            },
        )

    @app.get("/connectors/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
    async def run_page(request: Request, run_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        require()
        run = await runner.run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="No such run")
        reg = runner.get(run.connector)
        return templates.TemplateResponse(
            request,
            "core/connector_run.html",
            {
                "page_title": f"Run {run.id}",
                "run": run,
                "title": reg.decl.title if reg else run.connector,
                "status": _status(run),
                "started": _when(run.started_at),
                "finished": _when(run.finished_at),
                "counts": [{"outcome": k, "count": n} for k, n in run.counts.items()],
                "problems": run.problems,
            },
        )

    @app.get("/connectors/runs/{run_id}/progress", response_class=HTMLResponse,
             include_in_schema=False)  # fmt: skip
    async def run_progress(request: Request, run_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        """The live part of a running run's page (htmx polls it); once the run has ended,
        the page reloads to show the outcome."""
        require()
        run = await runner.run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="No such run")
        if run.status != "running":
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return templates.TemplateResponse(
            request,
            "core/connector_progress.html",
            {"run": run, "counts": [{"outcome": k, "count": n} for k, n in run.counts.items()]},
        )

    @app.post("/connectors/{key}/run", include_in_schema=False)
    async def start_run(key: str, dry_run: bool = Form(False)) -> Response:  # pyright: ignore[reportUnusedFunction]
        require()
        connector_or_404(key)
        if not dry_run:
            require_human()
        try:
            run_id = await runner.start(key, trigger="manual", dry_run=dry_run)
        except ConnectorError as exc:
            return back(f"/connectors/{key}", str(exc))
        return back(f"/connectors/runs/{run_id}")

    @app.post("/connectors/{key}/enable", include_in_schema=False)
    async def enable(key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        require()
        require_human()
        connector_or_404(key)
        await runner.set_enabled(key, True)
        return back(f"/connectors/{key}")

    @app.post("/connectors/{key}/disable", include_in_schema=False)
    async def disable(key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        require()
        connector_or_404(key)
        await runner.set_enabled(key, False)
        return back(f"/connectors/{key}")
