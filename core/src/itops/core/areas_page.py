"""``/access/<module>/<kind>/<key>``: who sees an area (ADR 0012), for admins in a browser.

Modules link here from their own pages (``ui.access_summary``): a Docs collection's edit
page, an asset type's page. The module's ``describe_area`` service names the area and
says where to go back to; an unknown area is a 404.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.areas import AREAS_PERMISSION, AreaError
from itops.core.forms import KeptForm
from itops.core.runtime import Runtime


def mount_access(app: FastAPI, templates: Jinja2Templates, runtime: Runtime) -> None:
    def admin() -> auth.Principal:
        try:
            principal = auth.require(AREAS_PERMISSION)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail="Only admins change access.") from exc
        if principal.via_ai:  # an AI connection never changes who sees what
            raise HTTPException(status_code=403, detail="Open this page in a browser.")
        return principal

    async def describe(module: str, kind: str, key: str) -> dict[str, str]:
        if kind not in runtime.areas.kinds.get(module, {}):
            raise HTTPException(status_code=404)
        fn = runtime.registry.get(f"{module}.describe_area")
        info: dict[str, str] | None = await fn(kind=kind, key=key) if fn else None
        if info is None:
            raise HTTPException(status_code=404)
        return info

    def page_url(module: str, kind: str, key: str) -> str:
        return f"/access/{module}/{kind}/{quote(key, safe='')}"

    @app.get("/access/{module}/{kind}/{key}", response_class=HTMLResponse, include_in_schema=False)
    async def access_page(  # pyright: ignore[reportUnusedFunction]
        request: Request, module: str, kind: str, key: str
    ) -> Response:
        who = admin()
        info = await describe(module, kind, key)
        current = await runtime.areas.get(module, kind, key)
        kept = runtime.forms.recall(who.user, request.query_params.get("kept"))
        groups = "\n".join(current.groups) if current else ""
        no_ai = bool(current and current.no_ai)
        if kept is not None:
            groups, no_ai = kept.get("groups"), kept.get("no_ai") == "1"
        return templates.TemplateResponse(
            request,
            "core/access.html",
            {
                "page_title": f"Access: {info['name']}",
                "current_module": "admin",
                "area_name": info["name"],
                "area_kind": runtime.areas.kinds[module][kind],
                "back": info["url"],
                "action": page_url(module, kind, key),
                "groups": groups,
                "no_ai": no_ai,
                "current": current,
                "error": request.query_params.get("error"),
            },
        )

    @app.post("/access/{module}/{kind}/{key}", include_in_schema=False)
    async def save_access(  # pyright: ignore[reportUnusedFunction]
        module: str,
        kind: str,
        key: str,
        groups: Annotated[str, Form()] = "",
        no_ai: Annotated[str, Form()] = "",
    ) -> Response:
        who = admin()
        info = await describe(module, kind, key)
        try:
            await runtime.areas.set(
                module, kind, key, name=info["name"], groups=[groups], no_ai=no_ai == "1"
            )
        except (AreaError, auth.PermissionDenied) as exc:
            params: dict[str, Any] = {"error": str(exc)}
            values = {"groups": [groups], "no_ai": [no_ai]}
            if token := runtime.forms.keep(who.user, KeptForm(values)):
                params["kept"] = token
            url = page_url(module, kind, key) + "?" + urlencode(params, quote_via=quote)
            return RedirectResponse(url, status_code=303)
        return RedirectResponse(info["url"], status_code=303)
