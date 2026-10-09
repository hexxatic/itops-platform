"""Deployment themes: a company's branding without touching core (design.md §8).

A theme is an installed package registered under the ``itops.themes`` entry point. It ships
``theme.css`` (overrides of the design tokens in ``tokens.css``, nothing else) and, optionally,
``theme.toml``::

    logo = "logo.png"          # shown in the header, top left
    logo_dark = "logo-rev.png" # optional: the logo for dark mode (e.g. a reversed wordmark)
    logo_alt = "Acme logo"
    subtitle = "Acme IT"       # under the product name

Core serves the theme's own files at ``/theme/<file>`` and the shell links them. With no theme
installed, IT-Ops shows its neutral defaults. ``ITOPS_THEME`` picks one by name when several
are installed, or ``none`` to switch theming off.

Appearance (light, dark, or follow the system) is each person's choice on /account. It is kept
in their session, set for the request by ``AppearanceMiddleware``, and the shell writes it to
``<html data-theme>``; tokens are ``light-dark()`` pairs, so nothing else changes.
"""

from __future__ import annotations

import hashlib
import logging
import tomllib
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import metadata, resources
from typing import Any, Literal

from starlette.types import ASGIApp, Receive, Scope, Send

ENTRY_POINT_GROUP = "itops.themes"
log = logging.getLogger(__name__)

_MEDIA = {".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp"}


@dataclass(frozen=True)
class Theme:
    name: str
    package: str
    logo: str | None = None
    logo_dark: str | None = None
    logo_alt: str = ""
    subtitle: str = ""

    def files(self) -> dict[str, str]:
        """Published file name -> media type (only these are served)."""
        out = {"theme.css": "text/css"}
        for logo in (self.logo, self.logo_dark):
            if logo:
                ext = "." + logo.rsplit(".", 1)[-1].lower()
                out[logo] = _MEDIA.get(ext, "application/octet-stream")
        return out

    def read(self, filename: str) -> bytes:
        return resources.files(self.package).joinpath(filename).read_bytes()

    def version(self) -> str:
        """Cache-buster: a hash of the theme's files."""
        digest = hashlib.sha256()
        for name in sorted(self.files()):
            digest.update(self.read(name))
        return digest.hexdigest()[:12]


def discover(setting: str = "") -> Theme | None:
    """The active theme: ``setting`` names one, ``"none"`` disables theming, and empty means
    the only installed theme (if exactly one is installed)."""
    if setting.strip().lower() == "none":
        return None
    found = {ep.name: ep for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    if setting:
        ep = found.get(setting)
        if ep is None:
            log.error("theme %r is not installed (installed: %s)", setting, sorted(found) or "none")
            return None
    elif len(found) == 1:
        ep = next(iter(found.values()))
    else:
        if found:
            log.warning("several themes installed (%s); set ITOPS_THEME", sorted(found))
        return None
    package = ep.value.split(":", 1)[0]
    config: dict[str, object] = {}
    toml = resources.files(package).joinpath("theme.toml")
    if toml.is_file():
        config = tomllib.loads(toml.read_text("utf-8"))
    theme = Theme(
        name=ep.name,
        package=package,
        logo=str(config["logo"]) if config.get("logo") else None,
        logo_dark=str(config["logo_dark"]) if config.get("logo_dark") else None,
        logo_alt=str(config.get("logo_alt") or ""),
        subtitle=str(config.get("subtitle") or ""),
    )
    if not resources.files(package).joinpath("theme.css").is_file():
        log.error("theme %r has no theme.css; ignoring it", ep.name)
        return None
    return theme


# ---- appearance: each person's light/dark choice --------------------------------------

Appearance = Literal["auto", "light", "dark"]
APPEARANCES: tuple[Appearance, ...] = ("auto", "light", "dark")
_appearance: ContextVar[Appearance] = ContextVar("itops_appearance", default="auto")


def parse_appearance(value: object) -> Appearance:
    return value if value in ("light", "dark") else "auto"  # pyright: ignore[reportReturnType]


def current_appearance() -> Appearance:
    """This request's appearance (templates: ``appearance()``)."""
    return _appearance.get()


class AppearanceMiddleware:
    """Pure ASGI (so the value is set in the handler's task). Sits inside SessionMiddleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session: dict[str, Any] = scope.get("session") or {}
        token = _appearance.set(parse_appearance(session.get("appearance")))
        try:
            await self.app(scope, receive, send)
        finally:
            _appearance.reset(token)
