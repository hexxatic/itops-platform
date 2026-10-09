"""Test tool actions (ADR 0011): one that runs until a test lets it finish, one that fails."""

from __future__ import annotations

import asyncio

from itops.sdk import ActionContext, ActionError, Module

module = Module(__name__)

# When set, slow_job waits for it after its first step (tests look at a run in progress).
GATE: asyncio.Event | None = None


@module.action("slow_job")
async def slow_job(ctx: ActionContext) -> None:
    ctx.step(f"Replicating {ctx.inputs['site']}", total=2)
    ctx.log("dc01: replicated")
    ctx.advance()
    if GATE is not None:
        await GATE.wait()
    ctx.log("dc02: replicated")
    ctx.advance()
    ctx.count("replicated", 2)


@module.action("breaks")
async def breaks(ctx: ActionContext) -> None:
    ctx.step("Step one")
    ctx.step("Step two")
    raise ActionError("the endpoint refused the connection")
