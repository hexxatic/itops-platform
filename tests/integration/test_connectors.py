"""Connectors end to end (ADR 0010): a test connector syncs devices into assets."""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import itops_assets
from itops_assets import service as assets
from sqlalchemy import text

from itops.core import connectors
from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.connectors import ConnectorError
from itops.sdk import Principal
from itops.testing.app import McpClient, ToolError, acting_as

sys.path.insert(0, str(Path(__file__).parent / "fakesync"))
import itops_fakesync

KEY = "fakesync.inventory"
ADMIN_VIA_AI = Principal(
    user="pat@example.com",
    display_name="Pat",
    permissions=frozenset({"*"}),
    via_ai=True,
    client="api",
)
EDITOR = Principal(
    user="ed", display_name="Ed", permissions=frozenset({"assets.read", "assets.write"})
)


def secret(name: str) -> str | None:
    return "secret" if name == "ITOPS_FAKESYNC_TOKEN" else None


def no_secret(_name: str) -> str | None:
    return None


@asynccontextmanager
async def running(
    database_url: str, tmp_path: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, Any]]:
    settings = Settings(
        database_url_override=database_url, files_dir=str(tmp_path), run_connectors=False
    )
    app = create_app(settings, modules=[itops_assets.module, itops_fakesync.module])
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        runtime.connectors.secrets = secret
        async with runtime.sessionmaker() as s:
            for table in ("source_refs", "connector_runs", "connector_state"):
                await s.execute(text(f"DELETE FROM itops.{table} WHERE connector = :k"), {"k": KEY})
            await s.execute(text("DELETE FROM assets.proposals"))
            await s.execute(text("DELETE FROM assets.records"))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", follow_redirects=False
        ) as c:
            yield c, runtime


async def run(runtime: Any, *, dry_run: bool = False) -> Any:
    run_id = await runtime.connectors.start(KEY, dry_run=dry_run)
    await runtime.connectors.wait(KEY)
    return await runtime.connectors.run(run_id)


async def devices() -> dict[str, dict[str, Any]]:
    async with itops_assets.module.session() as s:
        found = await assets.records(s, type_key="device")
        return {r.name: {"id": str(r.id), **assets.record_fields(r)} for r in found}


async def test_a_connector_syncs_into_assets(database_url: str, tmp_path: Path) -> None:
    tag = uuid.uuid4().hex[:6]
    itops_fakesync.SOURCE[:] = [
        {"id": "1", "name": f"SQL-{tag}",
         "fields": {"serial_number": "S1", "primary_ip": "10.0.0.1", "model": "R650"}},
        {"id": "2", "name": f"APP-{tag}", "fields": {"serial_number": "S2"}},
    ]  # fmt: skip
    async with running(database_url, tmp_path) as (c, runtime):
        page = (await c.get("/connectors")).text
        assert "Inventory sync" in page and "Off" in page and "Every 1m" in page

        # A dry run writes nothing but says what would happen.
        dry = await run(runtime, dry_run=True)
        assert dry.status == "succeeded" and dry.counts == {"created": 2}
        assert await devices() == {}

        first = await run(runtime)
        assert first.counts == {"created": 2}
        found = await devices()
        sql_id = found[f"SQL-{tag}"]["id"]
        assert found[f"SQL-{tag}"]["model"] == "R650"  # first import brings every value
        async with runtime.sessionmaker() as s:
            actor = (await s.execute(text(
                "SELECT TOP 1 actor_user FROM itops.audit_log WHERE entity_id = :i ORDER BY id"),
                {"i": sql_id})).scalar()  # fmt: skip
        assert actor == f"connector:{KEY}"  # acts as itself, not a person

        assert (await run(runtime)).counts == {"unchanged": 2}  # re-run: nothing to do

        # People can't change owned fields; they can change the rest.
        record = (await c.get(f"/assets/r/{sql_id}")).text
        assert "Synced from Inventory" in record
        form = (await c.get(f"/assets/r/{sql_id}/edit")).text
        assert "Synced from Inventory: change it there." in form
        with acting_as(EDITOR):
            await c.post(f"/api/assets/records/{sql_id}", data={
                "name": "renamed", "f__serial_number": "HACKED", "f__primary_ip": "10.0.0.1",
                "f__model": "R760", "f__notes": "Rack 4"})  # fmt: skip
        mine = (await devices())[f"SQL-{tag}"]
        assert (
            mine["serial_number"] == "S1" and mine["model"] == "R760" and mine["notes"] == "Rack 4"
        )

        # The source changes an owned field: updated, and people's edits stay.
        itops_fakesync.SOURCE[0]["fields"] = {
            "serial_number": "S1",
            "primary_ip": "10.0.0.9",
            "model": "R650",
        }
        changed = await run(runtime)
        assert changed.counts == {"updated": 1, "unchanged": 1}
        mine = (await devices())[f"SQL-{tag}"]
        assert (
            mine["primary_ip"] == "10.0.0.9"
            and mine["model"] == "R760"
            and mine["notes"] == "Rack 4"
        )

        # AI can't propose changes to owned fields.
        mcp = McpClient([itops_assets.module])
        try:
            await mcp.call(
                "assets_propose_change", id=sql_id, note="x", fields={"primary_ip": "1.1.1.1"}
            )
            raise AssertionError("expected a ToolError")
        except ToolError as exc:
            assert "synced from Inventory" in str(exc)
        await mcp.call("assets_propose_change", id=sql_id, note="ok", fields={"notes": "Rack 5"})

        # A record gone from the source is flagged for review, not deleted.
        itops_fakesync.SOURCE[:] = itops_fakesync.SOURCE[:1]
        gone = await run(runtime)
        assert gone.status == "problems" and gone.counts["missing from source"] == 1
        app_id = (await devices())[f"APP-{tag}"]["id"]
        assert "No longer in Inventory since" in (await c.get(f"/assets/r/{app_id}")).text

        run_page = (await c.get(f"/connectors/runs/{gone.id}")).text
        assert "no longer in Inventory" in run_page
        assert "missing from source" in (await c.get(f"/connectors/{KEY}")).text


async def test_scheduling_failures_and_admin_actions(database_url: str, tmp_path: Path) -> None:
    itops_fakesync.SOURCE[:] = []
    async with running(database_url, tmp_path) as (c, runtime):
        assert await runtime.connectors.tick() == []  # off: never scheduled
        r = await c.post(f"/connectors/{KEY}/enable")
        assert r.headers["location"] == f"/connectors/{KEY}"
        assert await runtime.connectors.tick() == [KEY]  # on and due (never ran)
        await runtime.connectors.wait(KEY)
        assert await runtime.connectors.tick() == []  # ran a moment ago

        # A missing secret fails the run with a readable problem.
        runtime.connectors.secrets = no_secret
        failed = await run(runtime)
        assert failed.status == "failed"
        assert "ITOPS_FAKESYNC_TOKEN isn't set" in failed.problems[-1]["text"]

        # Run now from the page; only admins see connectors.
        runtime.connectors.secrets = secret
        r = await c.post(f"/connectors/{KEY}/run", data={"dry_run": "true"})
        assert r.headers["location"].startswith("/connectors/runs/")
        await runtime.connectors.wait(KEY)
        with acting_as(EDITOR):
            assert (await c.get("/connectors")).status_code == 403
            assert (await c.post(f"/connectors/{KEY}/run")).status_code == 403
        # Tokens and AI tools (via AI) may dry-run, never really run or switch it on.
        with acting_as(ADMIN_VIA_AI):
            r = await c.post(f"/connectors/{KEY}/run", data={"dry_run": "true"})
            assert r.status_code == 303
            await runtime.connectors.wait(KEY)
            r = await c.post(f"/connectors/{KEY}/run")
            assert r.status_code == 403 and "Only a person" in r.text
            assert (await c.post(f"/connectors/{KEY}/enable")).status_code == 403
            try:
                await runtime.connectors.start(KEY)  # the second lock, behind the page
                raise AssertionError("expected a ConnectorError")
            except ConnectorError as exc:
                assert "Only a person" in str(exc)
        await c.post(f"/connectors/{KEY}/disable")
        assert await runtime.connectors.tick() == []


async def test_a_running_run_shows_its_progress(
    database_url: str, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(connectors, "PROGRESS_SECONDS", 0.05)
    tag = uuid.uuid4().hex[:6]
    itops_fakesync.SOURCE[:] = [
        {"id": f"p{i}-{tag}", "name": f"PROG-{i}-{tag}", "fields": {"kind": "Server"}}
        for i in range(3)
    ]
    gate = asyncio.Event()
    monkeypatch.setattr(itops_fakesync, "GATE", gate)
    async with running(database_url, tmp_path) as (client, runtime):
        run_id = await runtime.connectors.start(KEY)
        saved = await runtime.connectors.run(run_id)
        for _ in range(200):  # until the saved progress shows every item done
            saved = await runtime.connectors.run(run_id)
            if saved.progress.get("done") == 3:
                break
            await asyncio.sleep(0.05)
        assert saved.status == "running" and saved.counts.get("created") == 3
        assert saved.progress == {"step": "Devices", "number": 1, "done": 3, "total": 3}

        page = (await client.get(f"/connectors/runs/{run_id}")).text
        assert 'hx-get="/connectors/runs/' in page and "Step 1: Devices" in page
        live = await client.get(f"/connectors/runs/{run_id}/progress")
        assert 'value="3" max="3"' in live.text and "3 of 3" in live.text
        connector_page = (await client.get(f"/connectors/{KEY}")).text
        assert f'href="/connectors/runs/{run_id}">Watch its progress' in connector_page

        gate.set()
        await runtime.connectors.wait(KEY)
        done = await client.get(f"/connectors/runs/{run_id}/progress")
        assert done.headers.get("hx-refresh") == "true"  # the page reloads to the outcome
        assert "Outcome" in (await client.get(f"/connectors/runs/{run_id}")).text
    itops_fakesync.SOURCE[:] = []
