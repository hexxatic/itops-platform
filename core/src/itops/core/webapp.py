"""Installing IT-Ops as an app (Edge or Chrome: Install, then pin it to the taskbar).

Browsers offer to install a site that links a web app manifest with square icons of 192 and
512 pixels. Core serves the manifest at ``/manifest.webmanifest``, named after the site the
request is on (ADR 0027), with the theme's icons (``icons`` in ``theme.toml``) or core's
neutral ones. Both are public, like ``/static``: browsers fetch them without the sign-in
cookie. No service worker: IT-Ops is only useful online, and one would cache signed-in pages.
"""

from __future__ import annotations

import struct
from typing import Any

from itops.core.theme import Theme

DEFAULT_ICONS = (("/static/icon-192.png", 192), ("/static/icon-512.png", 512))
DEFAULT_COLOR = "#2b4c6e"  # the default icon's tile


def png_size(data: bytes) -> int | None:
    """A square PNG's width in pixels, else None (a manifest must state each icon's size)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width if width == height else None


def icons(theme: Theme | None) -> list[tuple[str, int]]:
    """(url, size) of the app's icons, smallest first: the theme's square PNGs, else core's."""
    found: list[tuple[str, int]] = []
    if theme is not None:
        version = theme.version()
        for name in theme.icons:
            size = png_size(theme.read(name))
            if size:
                found.append((f"/theme/{name}?v={version}", size))
    return sorted(found, key=lambda i: i[1]) or list(DEFAULT_ICONS)


def manifest(title: str, theme: Theme | None) -> dict[str, Any]:
    """The web app manifest for one site."""
    color = (theme.color if theme else "") or DEFAULT_COLOR
    return {
        "name": title,
        "short_name": title,
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "theme_color": color,
        "background_color": "#ffffff",
        "icons": [
            {"src": url, "sizes": f"{size}x{size}", "type": "image/png", "purpose": "any"}
            for url, size in icons(theme)
        ],
    }
