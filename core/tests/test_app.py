"""End-to-end: the real app with the real docs module, against the test database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from _helpers import MakeModule
from itops_docs import module as docs

from itops.core import auth
from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.mcp import build_server


@asynccontextmanager
async def running(database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """The app with its lifespan running, entered and exited in one task.

    Not a fixture: pytest-asyncio tears async fixtures down in a different task, which
    the MCP session manager's task group refuses.
    """
    app = create_app(Settings(database_url_override=database_url), modules=[docs])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


async def test_health_reports_modules(database_url: str) -> None:
    async with running(database_url) as client:
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["modules"] == {"docs": docs.manifest.version}
        assert r.json()["version"] == "dev"  # no release.json outside a release bundle
        assert r.json()["release"]["version"] == "dev"


@pytest.mark.parametrize("path", ["/", "/_design", "/docs/"])
async def test_pages_render_in_shell(database_url: str, path: str) -> None:
    async with running(database_url) as client:
        r = await client.get(path)
        assert r.status_code == 200
        assert 'class="shell-header"' in r.text
        assert 'href="/docs/"' in r.text  # module nav entry
        # Root-relative assets: absolute URLs would carry the app's internal address behind
        # the IIS proxy (http://127.0.0.1:port), which browsers can't load.
        assert 'href="/static/app.css?v=' in r.text
        assert "http://localhost/static" not in r.text


async def test_mcp_endpoint_is_mounted(database_url: str) -> None:
    async with running(database_url) as client:
        r = await client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert r.status_code == 200
        assert '"name":"itops"' in r.text.replace(" ", "")


async def test_mcp_tools_are_namespaced_and_run_via_ai(make_module: MakeModule) -> None:
    body = '''
from itops.core import auth  # test-only module: may peek at the principal

@module.tool(permission="mcpmod.read")
async def whoami() -> str:
    """Who is calling."""
    p = auth.current()
    return f"{p.user} via_ai={p.via_ai} client={p.client}"
'''
    mod = make_module("mcpmod", manifest_extra='[permissions]\n"mcpmod.read" = "Read"\n',
                      body=body)  # fmt: skip
    auth.configure(Settings())
    server = build_server([mod])
    assert [t.name for t in await server.list_tools()] == ["mcpmod_whoami"]
    result = await server.call_tool("mcpmod_whoami", {})
    assert "dev via_ai=True client=mcp" in str(result)
