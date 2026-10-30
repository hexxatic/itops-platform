"""Deployment themes (core/theme.py): served files, the shell's links, and the token-only lint."""

from __future__ import annotations

import struct
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from itops_docs import module as docs

from itops.core import app as app_module
from itops.core.app import create_app
from itops.core.config import Settings
from itops.core.lint import is_theme, lint_theme
from itops.core.theme import Theme, discover
from itops.core.webapp import png_size


def png(width: int, height: int = 0) -> bytes:
    """Just a PNG's header: what png_size reads."""
    header = struct.pack(">II", width, height or width)
    return bytes.fromhex("89504e470d0a1a0a0000000d49484452") + header + bytes(5)


CSS = (
    ":root {\n  --accent: #123456;\n}\n"
    "@media (prefers-color-scheme: dark) {\n  :root { --accent: #abcdef; }\n}\n"
)


@pytest.fixture
def theme(tmp_path: Path) -> Iterator[Theme]:
    pkg = tmp_path / "acme_theme"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", "utf-8")
    (pkg / "theme.css").write_bytes(CSS.encode())
    (pkg / "logo.png").write_bytes(b"\x89PNG fake")
    (pkg / "icon.png").write_bytes(png(512))
    sys.path.insert(0, str(tmp_path))
    try:
        yield Theme(
            name="acme",
            package="acme_theme",
            logo="logo.png",
            logo_alt="Acme",
            subtitle="Acme IT",
            icons=("icon.png", "logo.png"),
            color="#123456",
        )
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("acme_theme", None)


def test_theme_files_and_version(theme: Theme) -> None:
    assert theme.files() == {"theme.css": "text/css", "logo.png": "image/png",
                             "icon.png": "image/png"}  # fmt: skip
    assert theme.read("theme.css").decode() == CSS
    assert len(theme.version()) == 12


def test_none_switches_theming_off() -> None:
    assert discover("none") is None
    assert discover("not-installed") is None


async def test_shell_links_theme_and_serves_only_its_files(
    theme: Theme, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def active(_setting: str) -> Theme:
        return theme

    monkeypatch.setattr(app_module, "discover_theme", active)
    app = create_app(Settings(database_url_override=database_url), modules=[docs])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            page = (await c.get("/_design")).text
            assert f'href="/theme/theme.css?v={theme.version()}"' in page
            assert 'src="/theme/logo.png' in page and 'alt="Acme"' in page and "Acme IT" in page
            css = await c.get("/theme/theme.css")
            assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
            assert (await c.get("/theme/logo.png")).status_code == 200
            assert (await c.get("/theme/__init__.py")).status_code == 404


def test_lint_theme_allows_tokens_only(tmp_path: Path) -> None:
    root = tmp_path / "theme-acme"
    css = root / "src" / "acme_theme" / "theme.css"
    css.parent.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project.entry-points."itops.themes"]\nacme = "acme_theme"\n', "utf-8"
    )
    css.write_text("/* Acme\n   colors */\n" + CSS, "utf-8")
    assert is_theme(root)
    assert lint_theme(root) == []
    css.write_text(CSS + ".btn { color: red; }\n", "utf-8")
    assert [v.rule for v in lint_theme(root)] == ["theme-tokens"]
    (css.parent / "extra.css").write_text(":root {}\n", "utf-8")
    assert sorted(v.rule for v in lint_theme(root)) == ["theme-tokens", "theme-tokens"]


async def test_appearance_choice_pins_the_scheme(database_url: str) -> None:
    app = create_app(Settings(database_url_override=database_url), modules=[docs])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            assert "data-theme" not in (await c.get("/")).text  # follows the system
            r = await c.post("/account/appearance", data={"appearance": "dark"})
            assert r.status_code == 303
            assert '<html lang="en" data-theme="dark">' in (await c.get("/")).text
            assert 'value="dark" selected' in (await c.get("/account")).text
            bad = await c.post("/account/appearance", data={"appearance": "purple"})
            assert bad.status_code == 422
            await c.post("/account/appearance", data={"appearance": "auto"})
            assert "data-theme" not in (await c.get("/")).text


def test_png_size() -> None:
    assert png_size(png(192)) == 192
    assert png_size(png(300, 200)) is None  # not square: not an app icon
    assert png_size(b"GIF89a") is None


async def test_installable_as_an_app(
    theme: Theme, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_theme(_setting: str) -> None:
        return None

    monkeypatch.setattr(app_module, "discover_theme", no_theme)
    app = create_app(Settings(database_url_override=database_url), modules=[docs])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            r = await c.get("/manifest.webmanifest")
            assert r.headers["content-type"].startswith("application/manifest+json")
            body = r.json()
            assert (body["name"], body["start_url"], body["display"]) == ("IT-Ops", "/",
                                                                          "standalone")  # fmt: skip
            # No theme: core's neutral icons, which are really 192 and 512 square.
            assert [i["sizes"] for i in body["icons"]] == ["192x192", "512x512"]
            for icon in body["icons"]:
                got = await c.get(icon["src"])
                assert png_size(got.content) == int(icon["sizes"].split("x")[0])
            page = (await c.get("/_design")).text
            assert '<link rel="manifest" href="/manifest.webmanifest">' in page
            assert '<link rel="icon" type="image/png" href="/static/icon-192.png">' in page
            fav = await c.get("/favicon.ico", follow_redirects=False)
            assert fav.headers["location"] == "/static/icon-192.png"

    def active(_setting: str) -> Theme:
        return theme

    monkeypatch.setattr(app_module, "discover_theme", active)
    app = create_app(Settings(database_url_override=database_url), modules=[docs])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            body = (await c.get("/manifest.webmanifest")).json()
            # The theme's square PNG; its logo isn't one, so it's left out.
            assert [(i["src"].split("?")[0], i["sizes"]) for i in body["icons"]] == [
                ("/theme/icon.png", "512x512")
            ]
            assert body["theme_color"] == "#123456"
            assert '<meta name="theme-color" content="#123456">' in (await c.get("/_design")).text
