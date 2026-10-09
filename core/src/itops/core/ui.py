"""UI shell: Jinja environment, navigation, and module page routes.

Templates are namespaced: ``core/...`` for the shell and components, ``<module>/...``
for each module's bundled ``templates/`` folder (loaded from the package, so wheels
work). Modules extend ``core/shell.html`` and use ``core/components.html`` macros.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any, cast

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import (
    BaseLoader,
    ChoiceLoader,
    Environment,
    PackageLoader,
    PrefixLoader,
    select_autoescape,
)

from itops.core import auth
from itops.core.audit import Entry
from itops.core.config import Settings
from itops.core.release import release_version
from itops.core.theme import Theme, current_appearance
from itops.sdk import Module, PageSpec


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    module: str


def build_templates(
    modules: list[Module], settings: Settings, theme: Theme | None = None
) -> Jinja2Templates:
    prefixes: dict[str, BaseLoader] = {"core": PackageLoader("itops", "templates/core")}
    for mod in modules:
        # UI is optional: API/MCP-only modules have no templates folder.
        if resources.files(mod.package).joinpath("templates").is_dir():
            prefixes[mod.name] = PackageLoader(mod.package, "templates")
    env = Environment(
        loader=ChoiceLoader([PrefixLoader(prefixes)]),
        autoescape=select_autoescape(["html"]),
    )
    nav = [
        NavItem(page.nav, f"/{mod.name}{page.path}", mod.name)
        for mod in modules
        for page in mod.pages
        if page.nav
    ]
    if any(mod.manifest.actions for mod in modules):  # one Tools header (ADR 0011)
        nav.append(NavItem("Tools", "/tools", "tools"))
    # jinja2's stubs type globals narrowly; it's a plain dict at runtime.
    template_globals = cast(dict[str, Any], env.globals)
    template_globals.update(
        brand_name=settings.brand_name,
        app_version=release_version(),
        # Dev: recomputed per page (CSS edits don't restart the server). Prod: once.
        asset_version=asset_version if settings.is_dev else _fixed(asset_version()),
        nav_items=nav,
        current_user=auth.current,
        appearance=current_appearance,
        theme=theme,
        theme_version=theme.version() if theme else "",
    )
    env.filters["filesize"] = filesize
    return Jinja2Templates(env=env)


def _fixed(value: str) -> Callable[[], str]:
    return lambda: value


def asset_version() -> str:
    """Cache-buster for core static files: a hash of their contents.

    Browsers cache CSS/JS aggressively; the app version doesn't change when a stylesheet
    does, so links carry ``?v=<hash of the files>`` instead. Any edit, new link.
    """
    digest = hashlib.sha256()
    root = resources.files("itops").joinpath("static")
    stack = [root]
    while stack:
        node = stack.pop()
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            if child.is_dir():
                stack.append(child)
            elif child.name.endswith((".css", ".js")):
                digest.update(child.name.encode())
                digest.update(child.read_bytes())
    return digest.hexdigest()[:12]


def _page_endpoint(templates: Jinja2Templates, mod: Module, page: PageSpec) -> Any:
    async def endpoint(request: Request) -> Response:
        if page.permission:
            try:
                auth.require(page.permission)
            except auth.PermissionDenied as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        context = await page.handler(request)
        context.setdefault("page_title", page.title)
        context.setdefault("current_module", mod.name)
        return templates.TemplateResponse(request, f"{mod.name}/{page.template}", context)

    endpoint.__name__ = f"{mod.name}_page_{page.handler.__name__}"
    return endpoint


def mount_ui(app: FastAPI, templates: Jinja2Templates, modules: list[Module]) -> None:
    app.mount("/static", StaticFiles(packages=[("itops", "static")]), name="static")

    for mod in modules:
        router = APIRouter()
        for page in mod.pages:
            router.add_api_route(
                page.path,
                _page_endpoint(templates, mod, page),
                methods=["GET"],
                response_class=HTMLResponse,
                include_in_schema=False,
            )
        app.include_router(router, prefix=f"/{mod.name}")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return templates.TemplateResponse(
            request,
            "core/home.html",
            {"page_title": "Home", "modules": [m.manifest for m in modules]},
        )

    @app.get("/_design", response_class=HTMLResponse, include_in_schema=False)
    async def design(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return templates.TemplateResponse(
            request,
            "core/design.html",
            {"page_title": "Design system", "sample_activity": _sample_activity()},
        )


def _sample_activity() -> list[Entry]:
    at = dt.datetime(2026, 9, 30, 14, 5)
    return [
        Entry(3, at, "docs", "document.published", "document", "1", "Print server failover",
              "sam@example.com", "Sam Rivera", False, "web", "Published version 2", None),
        Entry(2, at - dt.timedelta(minutes=20), "docs", "document.edited", "document", "1",
              "Print server failover", "sam@example.com", "Sam Rivera", True, "mcp",
              "Saved draft version 2: added the DNS step", None),
    ]  # fmt: skip


def filesize(n: int | float | None) -> str:
    """1536 -> "1.5 KB" (for attachment lists)."""
    size = float(n or 0)
    for unit in ("bytes", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"
