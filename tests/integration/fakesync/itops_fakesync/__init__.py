"""A test connector (ADR 0010): devices from a pretend inventory, through assets.upsert."""

from __future__ import annotations

import asyncio
from typing import Any

from itops.sdk import ConnectorContext, Module

module = Module(__name__)

# What the pretend inventory holds; tests change it between runs.
SOURCE: list[dict[str, Any]] = []
# When set, the run waits for it after its items (tests look at a run in progress).
GATE: asyncio.Event | None = None


@module.connector("inventory")
async def inventory(ctx: ConnectorContext) -> None:
    ctx.secret("token")
    upsert = module.services.get("assets.upsert")
    assert upsert is not None
    ctx.step("Devices", total=len(SOURCE))
    for item in SOURCE:
        result = await upsert(
            source_type="device", source_id=item["id"], type_key="device",
            name=item["name"], fields=item["fields"],
        )  # fmt: skip
        ctx.apply(result, item=item["name"])
    if GATE is not None:
        await GATE.wait()
    ctx.finished("device")
