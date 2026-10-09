"""Build a real app for module tests, without modules importing core internals.

from itops.testing.app import make_app, McpClient
app = make_app(database_url, [module], files_dir=tmp_path)
mcp = McpClient([module])            # the platform's MCP server, in-process
result = await mcp.call("docs_list_collections")
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError as SdkToolError
from sqlalchemy import text

from itops.core import auth
from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.mcp import build_server
from itops.sdk import Module, Principal


def make_app(database_url: str, modules: list[Module], *, files_dir: str | Path) -> FastAPI:
    return create_app(
        Settings(database_url_override=database_url, files_dir=str(files_dir)),
        modules=modules,
    )


class ToolError(RuntimeError):
    """An MCP tool call returned an error (what an AI client would be shown)."""


class McpClient:
    """Calls tools through the real MCP server, so the via-AI principal and each tool's
    permission check apply exactly as they do for Claude or ChatGPT. Use it inside a
    running app (``make_app`` + lifespan) so modules are bound to the database."""

    def __init__(self, modules: list[Module], *, app: FastAPI | None = None) -> None:
        # Pass the running app to include the core tools (itops_search).
        self._server = build_server(modules, app.state.runtime if app is not None else None)

    async def tool_names(self) -> list[str]:
        return [t.name for t in await self._server.list_tools()]

    async def call(self, name: str, /, **arguments: Any) -> Any:
        try:
            result: Any = await self._server.call_tool(name, arguments)
        except SdkToolError as exc:
            # In-process calls raise; over HTTP the client gets an is_error result with
            # this same message. Either way it's what the AI would be shown.
            raise ToolError(str(exc)) from exc
        texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        if result.is_error:
            raise ToolError(" ".join(texts))
        if result.structured_content is not None:
            data = result.structured_content
            # Non-object return values are wrapped as {"result": ...} by the SDK.
            return data["result"] if set(data) == {"result"} else data
        return json.loads(texts[0]) if texts else None


async def wait_for_search(module: Module) -> None:
    """Full-Text indexing is asynchronous; wait until recent writes are searchable."""
    async with module.session() as s:
        await module.runtime.search.wait_until_current(s)


async def clear_search(module: Module) -> None:
    """Empty the whole search index (tests share one database)."""
    async with module.session() as s:
        await s.execute(text("DELETE FROM itops.search_passages"))
        await s.commit()


@contextmanager
def acting_as(principal: Principal) -> Generator[Principal]:
    """Run a block as a specific user (e.g. one without some permission)."""
    auth.set_current(principal)
    try:
        yield principal
    finally:
        auth.set_current(None)
