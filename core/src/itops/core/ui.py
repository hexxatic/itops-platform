"""UI shell: Jinja environment, navigation, and module page routes.

Templates are namespaced: ``core/...`` for the shell and components, ``<module>/...``
for each module's bundled ``templates/`` folder (loaded from the package, so wheels
work). Modules extend ``core/shell.html`` and use ``core/components.html`` macros.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any, cast

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
from itops.core.charts import chart_layout, money, number
from itops.core.config import Settings
from itops.core.identity import Identity
from itops.core.release import release_version
from itops.core.sites import Sites
from itops.core.theme import Theme, current_appearance
from itops.core.waiting import WaitingBoard, nav_counts
from itops.core.webapp import DEFAULT_COLOR
from itops.core.webapp import icons as app_icons
from itops.sdk import Module, PageSpec

TOOLS_NAV_ORDER = 900  # the one Tools header comes after every module


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    module: str


def nav_items(modules: list[Module]) -> list[NavItem]:
    """The shell's module nav: each module's ``nav`` page, by ``nav_order``, then Tools."""
    ordered = sorted(enumerate(modules), key=lambda im: (im[1].manifest.nav_order, im[0]))
    nav = [
        NavItem(page.nav, f"/{mod.name}{page.path}", mod.name)
        for _, mod in ordered
        for page in mod.pages
        if page.nav
    ]
    if any(mod.manifest.actions for mod in modules):  # one Tools header (ADR 0011)
        nav.append(NavItem("Tools", "/tools", "tools"))
    return nav


def build_templates(
    modules: list[Module],
    settings: Settings,
    theme: Theme | None = None,
    admin_links: list[dict[str, str]] | None = None,
    sites: Sites | None = None,
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
    nav = nav_items(modules)
    links = admin_links or []
    on = sites or Sites(settings.public_url, settings.brand_name)

    def my_admin_links() -> list[dict[str, str]]:
        who = auth.current()
        if who is None or on.current() is not None:  # a site has no Admin (ADR 0027)
            return []
        return [x for x in links if who.has(x["permission"])]

    def site_nav() -> list[NavItem]:
        """The menu of the site this request is on (ADR 0027)."""
        return [n for n in nav if on.shows(n.module)]

    # jinja2's stubs type globals narrowly; it's a plain dict at runtime.
    template_globals = cast(dict[str, Any], env.globals)
    template_globals.update(
        brand_name=settings.brand_name,
        app_version=release_version(),
        # Dev: recomputed per page (CSS edits don't restart the server). Prod: once.
        asset_version=asset_version if settings.is_dev else _fixed(asset_version()),
        nav_items=site_nav,
        site_title=on.title,
        nav_counts=nav_counts,
        my_admin_links=my_admin_links,
        current_user=auth.current,
        appearance=current_appearance,
        theme=theme,
        theme_version=theme.version() if theme else "",
        app_icons=app_icons(theme),
        app_color=(theme.color if theme else "") or DEFAULT_COLOR,
    )
    env.filters["filesize"] = filesize
    env.filters["money"] = money
    env.filters["number"] = number
    template_globals["chart_layout"] = chart_layout
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


def mount_ui(
    app: FastAPI,
    templates: Jinja2Templates,
    modules: list[Module],
    board: WaitingBoard,
    sites: Sites | None = None,
    identity: Identity | None = None,
) -> None:
    # Windows' registry can map .mjs to text/plain; browsers refuse ES modules served so
    # (the vendored PDF.js is .mjs).
    mimetypes.add_type("text/javascript", ".mjs")
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

    by_name = {m.name: m.manifest for m in modules}
    on = sites or Sites()

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        """The hub: what's waiting for you, then every module you can open. A site opens
        its first menu page; someone who may only use a site is sent to it (ADR 0027)."""
        site = on.current()
        shown = [n for n in nav_items(modules) if on.shows(n.module)]
        if site is not None:
            if not shown:
                raise HTTPException(status_code=404, detail="Nothing here yet")
            return RedirectResponse(shown[0].href, status_code=303)
        who = auth.current()
        if who is not None and identity is not None and identity.only_everyone(who.permissions):
            to = next((s for s in on.by_key.values() if s.modules), None)
            if to is not None:
                return RedirectResponse(to.url + "/", status_code=303)
        waiting = await board.for_current()
        titles = {n.module: n.label for n in nav_items(modules)}
        cards = [
            {"title": n.label, "href": n.href, "count": nav_counts().get(n.module, 0),
             "description": by_name[n.module].description if n.module in by_name
             else "Operational jobs you can run, and requests waiting to be run."}
            for n in shown
        ]  # fmt: skip
        return templates.TemplateResponse(
            request,
            "core/home.html",
            {
                "page_title": "Home",
                "waiting": [
                    {
                        "module": titles.get(w.module, w.module),
                        "title": {"text": w.title, "href": w.url},
                    }
                    for w in waiting
                ],
                "cards": cards,
            },
        )

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        """Every admin page you may open, in one place."""
        mine = cast(Callable[[], list[dict[str, str]]], templates.env.globals["my_admin_links"])()
        if not mine:
            raise HTTPException(status_code=404, detail="Not found")
        return templates.TemplateResponse(
            request,
            "core/admin.html",
            {
                "page_title": "Admin",
                "current_module": "admin",
                "rows": [
                    {
                        "page": {"text": x["label"], "href": x["href"]},
                        "description": x.get("description", ""),
                    }
                    for x in mine
                ],
            },
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
