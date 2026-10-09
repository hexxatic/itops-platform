"""Record-type pages (ADR 0008): ``/<module>/types`` for every module that uses types.

Admins list, view, draft, publish, discard and retire types here. The same pages serve
every module, so Docs and assets look and behave the same.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.runtime import Runtime
from itops.core.types import (
    FIELD_KINDS,
    TypeProblem,
    approve_permission,
    describe_changes,
    draft_permission,
)
from itops.sdk import Module

log = logging.getLogger(__name__)

STATUS_TONE = {
    "draft": "warning",
    "published": "success",
    "published, with draft changes": "info",
    "retired": "neutral",
}


def mount_types(
    app: FastAPI, templates: Jinja2Templates, runtime: Runtime, modules: list[Module]
) -> None:
    for mod in modules:
        if mod.manifest.uses_types:
            _mount_module(app, templates, runtime, mod.name, mod.manifest.title or mod.name)


def _mount_module(
    app: FastAPI, templates: Jinja2Templates, runtime: Runtime, module: str, title: str
) -> None:
    base, api = f"/{module}/types", f"/api/{module}/types"

    def require_draft() -> auth.Principal:
        try:
            return auth.require(draft_permission(module))
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Record types are for admins.") from exc

    def can_approve() -> bool:
        p = auth.current()
        return bool(p and p.has(approve_permission(module)) and not p.via_ai)

    def maker() -> Any:
        assert runtime.sessionmaker is not None
        return runtime.sessionmaker

    def page(request: Request, template: str, ctx: dict[str, Any], status: int = 200) -> Response:
        return templates.TemplateResponse(
            request,
            template,
            {"module": module, "module_title": title, "base": base, "api": api, **ctx},
            status_code=status,
        )

    async def link_targets() -> list[dict[str, str]]:
        """What link fields can point at: every loaded module's ``link_targets`` service
        (``[{target, label}]``, e.g. ``{"target": "assets.device", "label": "Assets: Device"}``)."""
        out: list[dict[str, str]] = []
        services = runtime.registry.keys()  # a registry, not a dict
        for key in services:
            fn = runtime.registry.get(key)
            if not key.endswith(".link_targets") or fn is None:
                continue
            try:
                found = fn()
                if inspect.isawaitable(found):
                    found = await found
                out += [{"target": str(t["target"]), "label": str(t["label"])} for t in found]
            except Exception:
                log.exception("link targets from %s failed", key)
        return out

    async def editor(
        request: Request,
        *,
        key: str | None,
        definition: dict[str, Any],
        locked: list[str],
        error: str | None = None,
        note: str = "",
    ) -> Response:
        return page(
            request,
            "core/type_edit.html",
            {
                "page_title": "New type" if key is None else f"Edit {definition.get('name', key)}",
                "key": key,
                "definition": definition,
                "locked": locked,
                "kinds": FIELD_KINDS,
                "targets": await link_targets(),
                "error": error,
                "note": note,
                "action": api if key is None else f"{api}/{key}/draft",
            },
            status=400 if error else 200,
        )

    @app.get(base, response_class=HTMLResponse, include_in_schema=False)
    async def list_page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        require_draft()
        async with maker()() as s:
            views = await runtime.types.list_types(s, module, include_drafts=True)
        show_retired = request.query_params.get("retired") == "1"
        retired = sum(1 for v in views if v.status == "retired")
        rows = [
            {
                "name": {"text": v.current.definition.name, "href": f"{base}/{v.key}"},
                "key": v.key,
                "status": {"badge": v.status.capitalize(), "tone": STATUS_TONE[v.status]},
                "fields": len(v.current.definition.fields),
                "description": v.current.definition.description,
            }
            for v in views
            if show_retired or v.status != "retired"
        ]
        return page(
            request,
            "core/types.html",
            {
                "page_title": f"{title} types",
                "rows": rows,
                "retired": retired,
                "show_retired": show_retired,
                "drafts": sum(1 for v in views if v.draft is not None),
                "can_approve": can_approve(),
                "published": request.query_params.get("published"),
                "error": request.query_params.get("error"),
            },
        )

    @app.post(f"{api}/publish-drafts", include_in_schema=False)
    async def publish_drafts() -> Response:  # pyright: ignore[reportUnusedFunction]
        """Publish every type that has a draft (each one checked and audited as if published
        on its own page). Human-only, like publishing one."""
        require_draft()
        published: list[str] = []
        problems: list[str] = []
        async with maker()() as s:
            views = await runtime.types.list_types(s, module, include_drafts=True)
        for v in views:
            if v.draft is None:
                continue
            try:
                async with maker()() as s:
                    await runtime.types.publish(s, module, v.key)
                    await s.commit()
                published.append(v.current.definition.name)
            except auth.PermissionDenied as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except TypeProblem as exc:
                problems.append(f"{v.current.definition.name}: {exc}")
        url = f"{base}?published={len(published)}"
        if problems:
            url += "&error=" + quote("Not published: " + "; ".join(problems))
        return RedirectResponse(url, status_code=303)

    @app.get(f"{base}/new", response_class=HTMLResponse, include_in_schema=False)
    async def new_page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        require_draft()
        return await editor(
            request, key=None, definition={"name": "", "description": "", "fields": []}, locked=[]
        )

    @app.get(f"{base}/{{key}}", response_class=HTMLResponse, include_in_schema=False)
    async def view_page(request: Request, key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        require_draft()
        async with maker()() as s:
            view = await runtime.types.get(s, module, key)
            history = await runtime.types.history(s, module, key)
            activity = await runtime.audit.history(
                s, module=module, entity_kind="type", entity_id=key, limit=30
            )
        if view is None:
            raise HTTPException(status_code=404)
        changes = (
            describe_changes(
                view.published.definition if view.published else None, view.draft.definition
            )
            if view.draft
            else []
        )
        return page(
            request,
            "core/type.html",
            {
                "page_title": view.current.definition.name,
                "view": view,
                "tone": STATUS_TONE[view.status],
                "changes": changes,
                "history": history,
                "activity": activity,
                "can_approve": can_approve(),
                "error": request.query_params.get("error"),
                "notice": request.query_params.get("notice"),
            },
        )

    async def module_service(name: str, *args: Any) -> Any:
        """Call the module's ``<module>.<name>`` service; None if it doesn't provide one."""
        fn = runtime.registry.get(f"{module}.{name}")
        if fn is None:
            return None
        result = fn(*args)
        return await result if inspect.isawaitable(result) else result

    @app.get(f"{base}/{{key}}/retire", response_class=HTMLResponse, include_in_schema=False)
    async def retire_page(request: Request, key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        """Retiring asks what happens to the records that use the type (the module does it:
        ``type_usage``, ``move_type``, ``archive_type`` services, each optional)."""
        require_draft()
        if not can_approve():
            raise HTTPException(status_code=403, detail="Retiring a type is for admins.")
        async with maker()() as s:
            view = await runtime.types.get(s, module, key)
            others = await runtime.types.list_types(s, module)
        if view is None or view.published is None or view.retired:
            raise HTTPException(status_code=404)
        usage = await module_service("type_usage", key)
        choices = [("keep", "Keep them as they are (they keep this type)")]
        if runtime.registry.get(f"{module}.move_type") is not None:
            choices += [(f"move:{v.key}", f"Move them to {v.current.definition.name}")
                        for v in others if v.key != key]  # fmt: skip
        if runtime.registry.get(f"{module}.archive_type") is not None:
            choices.append(("archive", "Archive them as well"))
        return page(
            request,
            "core/type_retire.html",
            {"page_title": f"Retire {view.current.definition.name}", "view": view,
             "usage": usage, "choices": choices},
        )  # fmt: skip

    @app.post(f"{api}/{{key}}/retire", include_in_schema=False)
    async def retire(key: str, then: Annotated[str, Form()] = "keep") -> Response:  # pyright: ignore[reportUnusedFunction]
        """Retire the type, after moving or archiving its records if asked."""
        if not can_approve():
            raise HTTPException(status_code=403, detail="Retiring a type is for admins.")
        notice = ""
        try:
            if then.startswith("move:"):
                to = then.removeprefix("move:")
                async with maker()() as s:
                    offered = {v.key for v in await runtime.types.list_types(s, module)}
                if to == key or to not in offered:
                    raise TypeProblem(f"{to!r} isn't a published type to move records to.")
                done = await module_service("move_type", key, to)
                if done is None:
                    raise TypeProblem("This module can't move records to another type.")
                notice = f"Moved {done.get('moved', 0)} record(s)."
                if done.get("kept"):
                    notice += (
                        f" {done['kept']} synced record(s) kept this type (a connector owns them)."
                    )
            elif then == "archive":
                done = await module_service("archive_type", key)
                if done is None:
                    raise TypeProblem("This module can't archive records.")
                notice = f"Archived {done.get('archived', 0)} record(s)."
            elif then != "keep":
                raise TypeProblem("Choose what happens to the records.")
            async with maker()() as s:
                await runtime.types.set_retired(s, module, key, True)
                await s.commit()
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:  # TypeProblem, or the module refusing (e.g. DocsError)
            if not isinstance(exc, TypeProblem):
                log.warning("retiring %s.%s: %s", module, key, exc)
            return RedirectResponse(f"{base}/{key}?error={quote(str(exc))}", status_code=303)
        url = f"{base}/{key}" + (f"?notice={quote(notice)}" if notice else "")
        return RedirectResponse(url, status_code=303)

    @app.get(f"{base}/{{key}}/edit", response_class=HTMLResponse, include_in_schema=False)
    async def edit_page(request: Request, key: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        require_draft()
        async with maker()() as s:
            view = await runtime.types.get(s, module, key)
        if view is None:
            raise HTTPException(status_code=404)
        working = (view.draft or view.current).definition
        locked = [f.key for f in view.published.definition.fields] if view.published else []
        return await editor(request, key=key, definition=working.model_dump(), locked=locked)

    async def save(
        request: Request, key: str | None, name: str, description: str, fields: str, note: str
    ) -> Response:
        require_draft()
        try:
            parsed: Any = json.loads(fields or "[]")
        except json.JSONDecodeError:
            parsed = []
        submitted: dict[str, Any] = {"name": name, "description": description, "fields": parsed}
        try:
            async with maker()() as s:
                view = await runtime.types.draft(s, module, submitted, key=key, note=note)
                await s.commit()
        except TypeProblem as exc:
            locked: list[str] = []
            if key:
                async with maker()() as s:
                    current = await runtime.types.get(s, module, key)
                if current and current.published:
                    locked = [f.key for f in current.published.definition.fields]
            return await editor(
                request, key=key, definition=submitted, locked=locked, error=str(exc), note=note
            )
        return RedirectResponse(f"{base}/{view.key}", status_code=303)

    @app.post(api, include_in_schema=False)
    async def create(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        name: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        fields: Annotated[str, Form()] = "[]",
        note: Annotated[str, Form()] = "",
    ) -> Response:
        return await save(request, None, name, description, fields, note)

    @app.post(f"{api}/{{key}}/draft", include_in_schema=False)
    async def update(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        key: str,
        name: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        fields: Annotated[str, Form()] = "[]",
        note: Annotated[str, Form()] = "",
    ) -> Response:
        return await save(request, key, name, description, fields, note)

    async def lifecycle(key: str, action: str) -> Response:
        try:
            async with maker()() as s:
                if action == "publish":
                    await runtime.types.publish(s, module, key)
                elif action == "discard":
                    if await runtime.types.discard(s, module, key) == "deleted":
                        await s.commit()
                        return RedirectResponse(base, status_code=303)
                else:
                    await runtime.types.set_retired(s, module, key, action == "retire")
                await s.commit()
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TypeProblem as exc:
            return RedirectResponse(f"{base}/{key}?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"{base}/{key}", status_code=303)

    def lifecycle_route(action: str) -> Any:
        # A factory, not a default argument: FastAPI would expose a default as a query
        # parameter, letting a request pick a different action.
        async def endpoint(key: str) -> Response:
            return await lifecycle(key, action)

        return endpoint

    for action in ("publish", "discard", "restore"):  # retire: its own route, above
        app.add_api_route(
            f"{api}/{{key}}/{action}",
            lifecycle_route(action),
            methods=["POST"],
            include_in_schema=False,
        )
