"""Sites (ADR 0027): IT-Ops answering on another host name as a site of its own, e.g. the
help site for staff.

A site is configured in ``ITOPS_SITES`` (key -> {url, title}) and made of the modules
whose manifest says ``site = "<key>"``. A request's site comes from the host the browser
used. On a site's host only its modules' pages and API answer, with the plumbing every page
needs (sign-in, files, static files, the account page, search, health); anything else is
404. Its menu, header title and search hold only its modules. On IT-Ops' own host, site
modules stay out of the menu and search.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from itops.core.config import Settings
from itops.core.entra import public_host
from itops.sdk import Module

# What a site's host serves besides its modules (prefixes of the path).
SITE_PLUMBING = ("/health", "/static/", "/theme/", "/auth/", "/files/", "/account", "/search",
                 "/favicon.ico", "/manifest.webmanifest")  # fmt: skip

_current: ContextVar[str] = ContextVar("itops_site", default="")


@dataclass(frozen=True)
class Site:
    key: str
    url: str  # e.g. https://help.example.com, no trailing slash
    title: str
    modules: tuple[str, ...]

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc.lower()


@dataclass
class Sites:
    """The configured sites, and which one the current request is for."""

    public_url: str = ""
    brand_name: str = "IT-Ops"
    by_key: dict[str, Site] = field(default_factory=dict[str, Site])

    @classmethod
    def build(cls, settings: Settings, modules: list[Module]) -> Sites:
        found = cls(settings.public_url.rstrip("/"), settings.brand_name)
        for key, conf in settings.sites.items():
            members = tuple(m.name for m in modules if m.manifest.site == key)
            title = conf.title or key.title()
            found.by_key[key] = Site(key, conf.url.rstrip("/"), title, members)
        return found

    def problems(self, modules: list[Module]) -> list[str]:
        """Modules naming a site that isn't configured (their pages answer nowhere)."""
        return [
            f"{m.name} is part of site {m.manifest.site!r}, which ITOPS_SITES doesn't define"
            for m in modules
            if m.manifest.site and m.manifest.site not in self.by_key
        ]

    def for_host(self, host: str) -> Site | None:
        host = host.lower()
        return next((s for s in self.by_key.values() if s.host == host), None)

    @property
    def hosts(self) -> set[str]:
        return {s.host for s in self.by_key.values()}

    @property
    def site_modules(self) -> set[str]:
        """Every module that belongs to a site (kept out of IT-Ops' own menu and search)."""
        return {m for s in self.by_key.values() for m in s.modules}

    def current(self) -> Site | None:
        key = _current.get()
        return self.by_key.get(key) if key else None

    def title(self) -> str:
        site = self.current()
        return site.title if site else self.brand_name

    def base_url(self) -> str:
        """The address of the site this request is on (IT-Ops' own outside a site)."""
        site = self.current()
        return site.url if site else self.public_url

    def shows(self, module: str) -> bool:
        """Whether this module belongs in the current site's menu and search."""
        site = self.current()
        return module in site.modules if site else module not in self.site_modules

    def search_scope(self) -> tuple[bool, list[str]]:
        """For search: (True, modules) to search only those, (False, modules) to leave
        them out. On IT-Ops' host that's every site module; none configured, nothing."""
        site = self.current()
        if site is not None:
            return True, list(site.modules)
        return False, sorted(self.site_modules)


class SiteGate:
    """Pure ASGI middleware, outside the sign-in gate: decides the request's site and
    answers 404 on a site's host for anything that isn't the site's."""

    def __init__(self, app: ASGIApp, sites: Sites) -> None:
        self.app = app
        self.sites = sites

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.sites.by_key:
            await self.app(scope, receive, send)
            return
        site = self.sites.for_host(public_host(scope, Request(scope)))
        if site is not None and not _serves(site, scope["path"]):
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        token = _current.set(site.key if site else "")
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)


def _serves(site: Site, path: str) -> bool:
    if path == "/" or path.startswith(SITE_PLUMBING):
        return True
    first = path.removeprefix("/api/").lstrip("/").split("/", 1)[0]
    return first in site.modules
