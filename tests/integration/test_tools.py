"""Tools end to end (ADR 0011): live progress, one run at a time (with who is running it),
AI requests that a person approves, cancel, failures, search and audit."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from itops.core import actions as actions_module
from itops.core import auth
from itops.core.actions import ActionBusy, ActionError
from itops.testing.app import McpClient, ToolError, make_app

sys.path.insert(0, str(Path(__file__).parent / "faketools"))
import itops_faketools

SLOW = "faketools.slow_job"
BREAKS = "faketools.breaks"


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, Any]]:
    app = make_app(database_url, [itops_faketools.module], files_dir=tmp_path)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with runtime.sessionmaker() as s:
            await s.execute(text("DELETE FROM itops.action_runs WHERE action LIKE 'faketools.%'"))
            await s.execute(
                text("DELETE FROM itops.action_requests WHERE action LIKE 'faketools.%'")
            )
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c, app


async def wait_for(check: Any, tries: int = 100) -> None:
    for _ in range(tries):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_progress_one_at_a_time_and_who_is_running_it(
    database_url: str, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(actions_module, "PROGRESS_SECONDS", 0.05)
    gate = asyncio.Event()
    monkeypatch.setattr(itops_faketools, "GATE", gate)
    async with running(database_url, tmp_path) as (c, app):
        runner = app.state.runtime.actions
        tools = (await c.get("/tools")).text
        assert 'href="/tools"' in tools and "Fake tools" in tools and "Slow job" in tools
        page = (await c.get(f"/tools/{SLOW}")).text
        assert "Run the slow job?" in page and "South" in page and "DC01/ITOps.Fake" in page

        r = await c.post(f"/tools/{SLOW}/run", data={"in__site": "North"})
        assert r.status_code == 303 and r.headers["location"].startswith("/tools/runs/")
        run_id = int(r.headers["location"].rsplit("/", 1)[1])

        async def progressed() -> bool:
            run = await runner.run(run_id)
            return run is not None and "dc01: replicated" in run.log

        await wait_for(progressed)
        live = (await c.get(f"/tools/runs/{run_id}/progress")).text
        assert (
            "Step 1: Replicating North" in live and "1 of 2" in live and "dc01: replicated" in live
        )
        assert 'hx-trigger="every 2s"' in (await c.get(f"/tools/runs/{run_id}")).text

        # Someone else tries to start it: told who is running it, and sent to watch.
        again = await c.post(f"/tools/{SLOW}/run", data={"in__site": "South"})
        assert again.headers["location"].startswith(f"/tools/runs/{run_id}?error=")
        watching = (await c.get(again.headers["location"])).text
        assert "already running: started by Local developer" in watching
        assert "is running now" in (await c.get(f"/tools/{SLOW}")).text
        try:
            await runner.start(SLOW, {"site": "North"})
            raise AssertionError("a second run started")
        except ActionBusy as exc:
            assert exc.run is not None and exc.run.id == run_id

        gate.set()
        await runner.wait(run_id)
        done = await runner.run(run_id)
        assert done is not None and done.status == "succeeded" and done.counts == {"replicated": 2}
        assert (await c.get(f"/tools/runs/{run_id}/progress")).headers.get("hx-refresh") == "true"
        outcome = (await c.get(f"/tools/runs/{run_id}")).text
        assert "Succeeded" in outcome and "dc02: replicated" in outcome


async def test_ai_requests_a_person_runs(database_url: str, tmp_path: Path) -> None:
    async with running(database_url, tmp_path) as (c, app):
        runner = app.state.runtime.actions
        mcp = McpClient([itops_faketools.module], app=app)
        listed = await mcp.call("itops_list_tools")
        assert [t["key"] for t in listed] == [BREAKS, SLOW]
        # AI can't start a tool...
        with auth.acting_via_ai("mcp"):
            try:
                await runner.start(SLOW, {"site": "North"})
                raise AssertionError("AI started a tool")
            except auth.PermissionDenied:
                pass
        # ...but can request one, with checked inputs.
        try:
            await mcp.call("itops_request_tool_run", key=SLOW, reason="x", inputs={"site": "Mars"})
            raise AssertionError("bad input accepted")
        except ToolError as exc:
            assert "choose one of" in str(exc)
        asked = await mcp.call(
            "itops_request_tool_run", key=SLOW, reason="new DC", inputs={"site": "South"}
        )
        tools = (await c.get("/tools")).text
        assert "Waiting for someone to run" in tools and "new DC" in tools and "via AI" in tools

        r = await c.post(f"/tools/requests/{asked['request_id']}/approve")
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        await runner.wait(run_id)
        run = await runner.run(run_id)
        assert (
            run is not None and run.status == "succeeded" and run.request_id == asked["request_id"]
        )
        seen = await mcp.call("itops_get_tool_run", run_id=run_id)
        assert seen["status"] == "succeeded" and "dc02: replicated" in seen["output_tail"]

        declined = await mcp.call("itops_request_tool_run", key=SLOW, reason="again",
                                  inputs={"site": "North"})  # fmt: skip
        r = await c.post(f"/tools/requests/{declined['request_id']}/decline")
        assert "declined" in r.headers["location"].lower()
        req = await runner.get_request(declined["request_id"])
        assert req is not None and req.status == "declined" and req.run_id is None


async def test_failures_cancel_inputs_search_and_audit(
    database_url: str, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(actions_module, "PROGRESS_SECONDS", 0.05)
    gate = asyncio.Event()
    monkeypatch.setattr(itops_faketools, "GATE", gate)
    async with running(database_url, tmp_path) as (c, app):
        runtime = app.state.runtime
        runner = runtime.actions
        # A failing step: the run says why.
        run_id = await runner.start(BREAKS)
        await runner.wait(run_id)
        failed = await runner.run(run_id)
        assert failed is not None and failed.status == "failed"
        assert failed.problems[0]["text"] == "the endpoint refused the connection"
        assert "Step two" in (await c.get(f"/tools/runs/{run_id}")).text

        # Bad and missing inputs never start anything.
        r = await c.post(f"/tools/{SLOW}/run", data={"in__site": "Mars"})
        assert r.headers["location"].startswith(f"/tools/{SLOW}?error=")
        try:
            await runner.start(SLOW, {})
            raise AssertionError("missing input accepted")
        except ActionError as exc:
            assert "Site is required" in str(exc)

        # Cancel.
        run_id = await runner.start(SLOW, {"site": "North"})

        async def started() -> bool:
            run = await runner.run(run_id)
            return run is not None and bool(run.log)

        await wait_for(started)
        await c.post(f"/tools/runs/{run_id}/cancel")
        await runner.wait(run_id)
        cancelled = await runner.run(run_id)
        assert cancelled is not None and cancelled.status == "cancelled"

        # Searchable, and every run is in the audit trail.
        async with runtime.sessionmaker() as s:
            await runtime.search.wait_until_current(s)
            hits = await runtime.search.query(s, "slow job", principal=auth.current())
            assert any(h.url == f"/tools/{SLOW}" for h in hits)
            actions = (
                (
                    await s.execute(
                        text(
                            "SELECT action FROM itops.audit_log WHERE entity_kind = 'tool'"
                            " AND entity_id LIKE 'faketools.%'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert {"tool.run_started", "tool.run_finished"} <= set(actions)
