"""MCP aggregator: every module's tools behind one MCP endpoint (AI tier 1).

Users connect their own AI client to ``/mcp``. Tools are namespaced
``<module>_<tool>`` (underscores, not dots: several clients reject dots in tool
names). Each call runs as the connected user *via AI* (so human-only permissions
such as ``docs.publish`` are refused) and enforces the tool's declared permission.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from itops import __version__
from itops.core import auth
from itops.core.actions import ActionError
from itops.core.runtime import Runtime
from itops.core.types import FIELD_KINDS
from itops.sdk import AsyncFn, Module

INSTRUCTIONS = (
    "IT-Ops is an internal IT platform made of modules (tools are prefixed with the "
    "module name, e.g. docs_search; itops_search searches every module). Tools act with "
    "the connected user's permissions. "
    "Tools that change data create drafts for human review unless stated otherwise."
)


def _permission_checked(fn: AsyncFn, permission: str | None) -> AsyncFn:
    """Enforce ``permission`` on each call; ``functools.wraps`` keeps the signature
    so the SDK still derives the tool's input schema from ``fn``.

    Errors meant for the caller (``ValueError`` for bad input, ``PermissionError`` for
    access) are passed to the AI client with their message, so it can correct itself.
    Anything else stays a generic error: internals don't leak to clients.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        with auth.acting_via_ai("mcp"):
            try:
                if permission is not None:  # core tools filter by permission themselves
                    auth.require(permission)
                elif auth.current() is None:
                    raise auth.PermissionDenied("not signed in")
                return await fn(*args, **kwargs)
            except (ValueError, PermissionError) as exc:
                raise ToolError(str(exc)) from exc

    return wrapper


def build_server(
    modules: list[Module],
    runtime: Runtime | None = None,
    *,
    token_verifier: TokenVerifier | None = None,
    auth_settings: AuthSettings | None = None,
) -> MCPServer:
    server = MCPServer(
        name="itops",
        version=__version__,
        instructions=INSTRUCTIONS,
        token_verifier=token_verifier,
        auth=auth_settings,
    )
    for mod in modules:
        for tool in mod.tools:
            server.add_tool(
                _permission_checked(tool.fn, tool.permission),
                name=f"{mod.name}_{tool.name}",
                description=tool.description,
            )
    if runtime is not None:
        _add_core_tools(server, runtime)
        typed = sorted(m.name for m in modules if m.manifest.uses_types)
        if typed:
            _add_type_tools(server, runtime, typed)
    return server


def _add_core_tools(server: MCPServer, runtime: Runtime) -> None:
    async def itops_search(
        query: str, limit: int = 10, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Search everything in IT-Ops you're allowed to read (docs, and later tickets,
        assets, ...), best match first. Each result has the title, where in it the match
        is, a snippet and a `url` to cite. Only published content is searchable. To
        search one module only, use that module's own search tool (e.g. docs_search).
        Archived content is left out unless include_archived=true (results say which)."""
        maker = runtime.sessionmaker
        assert maker is not None
        async with maker() as s:
            hits = await runtime.search.query(
                s,
                query,
                principal=auth.current(),
                limit=max(1, min(limit, 30)),
                include_archived=include_archived,
            )
        base = runtime.settings.public_url.rstrip("/")
        return [{**h.as_dict(), "url": base + h.url} for h in hits]

    server.add_tool(
        _permission_checked(itops_search, None),
        name="itops_search",
        description=inspect.getdoc(itops_search) or "",
    )
    if runtime.actions.all():
        _add_action_tools(server, runtime)


def _add_action_tools(server: MCPServer, runtime: Runtime) -> None:
    """Tools (ADR 0011): AI can see them and their runs, and request a run; a person
    who may run it approves on /tools."""
    runner = runtime.actions
    base = runtime.settings.public_url.rstrip("/")

    async def itops_list_tools() -> list[dict[str, Any]]:
        """The tools (jobs IT-Ops can run, e.g. "Sync AD now") you may run or request: key,
        title, description, inputs (key, label, kind, options), danger level, and the
        last run's outcome. AI can't start a tool; use itops_request_tool_run."""
        last = await runner.last_runs()
        out: list[dict[str, Any]] = []
        for a in runner.visible(auth.current()):
            run = last.get(a.key)
            out.append({
                "key": a.key, "title": a.decl.title, "provider": a.module_title,
                "description": a.decl.description, "danger": a.decl.danger,
                "inputs": [i.model_dump() for i in a.decl.inputs],
                "last_run": None if run is None else {
                    "id": run.id, "status": run.status, "started_at": run.started_at.isoformat(),
                    "by": run.started_by_name, "summary": run.summary},
                "url": f"{base}/tools/{a.key}",
            })  # fmt: skip
        return out

    async def itops_request_tool_run(
        key: str, reason: str, inputs: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Ask for a tool to be run (key from itops_list_tools, inputs by their keys, and
        why). It doesn't run yet: it waits on the Tools page until someone allowed to run
        it approves. Give them the review_url."""
        try:
            request_id = await runner.request(key, inputs or {}, reason)
        except ActionError as exc:
            raise ValueError(str(exc)) from exc
        return {"request_id": request_id, "status": "pending", "review_url": f"{base}/tools"}

    async def itops_get_tool_run(run_id: int) -> dict[str, Any]:
        """One tool run: status (running, succeeded, problems, failed, cancelled), who
        started it, its inputs, progress, problems, summary and the end of its output."""
        run = await runner.run(run_id)
        action = runner.get(run.action) if run else None
        who = auth.current()
        if run is None or action is None or who is None or not who.holds(action.decl.permission):
            raise ValueError(f"No tool run {run_id} that you can see")
        return {
            "id": run.id, "tool": action.decl.title, "status": run.status,
            "started_by": run.started_by_name, "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "inputs": run.inputs, "progress": run.progress, "counts": run.counts,
            "problems": run.problems, "summary": run.summary,
            "output_tail": "\n".join(run.log.splitlines()[-60:]),
            "url": f"{base}/tools/runs/{run.id}",
        }  # fmt: skip

    for fn in (itops_list_tools, itops_request_tool_run, itops_get_tool_run):
        server.add_tool(
            _permission_checked(fn, None), name=fn.__name__, description=inspect.getdoc(fn) or ""
        )


def _add_type_tools(server: MCPServer, runtime: Runtime, modules: list[str]) -> None:
    """Record types (ADR 0008): read them, and draft new ones or changes. Drafts take
    effect only when an admin publishes them in the web UI (human-only)."""
    kinds = "; ".join(f"{k}: {v}" for k, v in FIELD_KINDS.items())

    def _module(module: str) -> str:
        if module not in modules:
            raise ValueError(
                f"'{module}' has no record types. Modules with types: {', '.join(modules)}"
            )
        return module

    async def itops_list_types(module: str, include_drafts: bool = False) -> list[dict[str, Any]]:
        maker = runtime.sessionmaker
        assert maker is not None
        auth.require(f"{_module(module)}.read")
        async with maker() as s:
            views = await runtime.types.list_types(s, module, include_drafts=include_drafts)
        return [
            {
                "key": v.key,
                "name": v.current.definition.name,
                "status": v.status,
                "description": v.current.definition.description,
                "fields": len(v.current.definition.fields),
            }
            for v in views
        ]

    async def itops_get_type(module: str, key: str, include_draft: bool = False) -> dict[str, Any]:
        maker = runtime.sessionmaker
        assert maker is not None
        auth.require(f"{_module(module)}.read")
        async with maker() as s:
            view = await runtime.types.get(s, module, key)
        if view is None:
            raise ValueError(f"No type '{key}' in {module}")
        return view.as_dict(include_draft=include_draft)

    async def itops_draft_type(
        module: str,
        name: str,
        fields: list[dict[str, Any]],
        description: str = "",
        key: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        maker = runtime.sessionmaker
        assert maker is not None
        definition = {"name": name, "description": description, "fields": fields}
        async with maker() as s:
            view = await runtime.types.draft(s, _module(module), definition, key=key, note=note)
            await s.commit()
        base = runtime.settings.public_url.rstrip("/")
        return {
            **view.as_dict(include_draft=True),
            "review_url": f"{base}/{module}/types/{view.key}",
        }

    for fn, description in (
        (itops_list_types,
         f"List the record types of a module ({', '.join(modules)}): key, name, status, "
         "description, field count. Published types only unless include_drafts=true."),
        (itops_get_type,
         "Read one record type: its fields (key, label, kind, required, help, options, "
         "multiple, target). Shows the published version; include_draft=true shows pending "
         "draft changes."),
        (itops_draft_type,
         "Draft a new record type (key omitted) or changes to an existing one (pass its key "
         "and the COMPLETE field list; read it first with itops_get_type). The draft has no "
         "effect until an admin publishes it in the web UI; give them the review_url. Each "
         "field: key (lowercase_with_underscores, fixed once published), label, kind, "
         "required, help; options for select; multiple for select/link; target for link "
         f"('docs', 'assets.device'). Kinds: {kinds}. A published field's kind can't change. "
         "Never put secrets in a type; use a credential field (a 1Password reference)."),
    ):  # fmt: skip
        server.add_tool(_permission_checked(fn, None), name=fn.__name__, description=description)
