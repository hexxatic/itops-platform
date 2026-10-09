"""Core file store, end to end: upload -> stored once -> permission-checked download."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from _helpers import MakeModule

from itops.core import auth
from itops.core.app import create_app
from itops.core.config import Settings
from itops.sdk import Module

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
    b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
PERMS = (
    "[permissions]\n"
    '"filemod.read" = "Read"\n'
    '"filemod.write" = "Write"\n'
    '"filemod.secret" = "Only some people"\n'
)
BODY = """
module.files.upload_route(permission="filemod.write")
module.files.upload_route(permission="filemod.write", path="/secret-files",
                          read_permission="filemod.secret")

@module.tool(permission="filemod.read")
async def noop() -> str:
    '''No-op.'''
    return "ok"
"""


@asynccontextmanager
async def running(url: str, files_dir: Path, mod: Module) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(database_url_override=url, files_dir=str(files_dir)), modules=[mod])
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


async def test_upload_download_roundtrip(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("filemod", manifest_extra=PERMS, body=BODY)
    async with running(database_url, tmp_path, mod) as c:
        r1 = await c.post("/api/filemod/files", files={"file": ("shot.png", PNG, "image/png")})
        r2 = await c.post("/api/filemod/files", files={"file": ("again.png", PNG, "image/png")})
        assert r1.status_code == 200, r1.text
        assert r1.json()["url"].startswith("/files/")
        # Same bytes -> one file on disk, two records.
        assert len(list(tmp_path.rglob("*.png"))) == 1
        assert r1.json()["id"] != r2.json()["id"]

        got = await c.get(r1.json()["url"])
        assert got.status_code == 200
        assert got.content == PNG
        assert got.headers["content-type"] == "image/png"
        assert got.headers["x-content-type-options"] == "nosniff"

        assert (await c.get("/files/00000000-0000-0000-0000-000000000000")).status_code == 404
        assert (await c.get("/files/not-a-uuid")).status_code == 404


async def test_type_allowlist_rejects_svg(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("filemod", manifest_extra=PERMS, body=BODY)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    async with running(database_url, tmp_path, mod) as c:
        r = await c.post("/api/filemod/files", files={"file": ("x.svg", svg, "image/svg+xml")})
        assert r.status_code == 400
        assert "not allowed" in r.json()["detail"]


async def test_download_checks_the_files_read_permission(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("filemod", manifest_extra=PERMS, body=BODY)
    async with running(database_url, tmp_path, mod) as c:
        up = await c.post("/api/filemod/secret-files", files={"file": ("s.png", PNG, "image/png")})
        url = up.json()["url"]
        limited = auth.Principal(
            user="bob", display_name="Bob", permissions=frozenset({"filemod.read"})
        )
        auth.set_current(limited)
        try:
            assert (await c.get(url)).status_code == 403
        finally:
            auth.set_current(None)
        assert (await c.get(url)).status_code == 200  # dev user has everything


async def test_claim_attaches_unclaimed_uploads(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("filemod", manifest_extra=PERMS, body=BODY)
    async with running(database_url, tmp_path, mod) as c:
        fid = (
            await c.post("/api/filemod/files", files={"file": ("a.png", PNG, "image/png")})
        ).json()["id"]
        async with mod.session() as s:
            assert await mod.files.claim(s, [fid], entity_type="thing", entity_id="1") == 1
            # Already claimed: a second claim (e.g. by another entity) does nothing.
            assert await mod.files.claim(s, [fid], entity_type="thing", entity_id="2") == 0
            await s.commit()


ATT_BODY = (
    BODY
    + """
module.files.upload_route(permission="filemod.write", path="/attachments", attachment=True)
"""
)


async def attach(c: httpx.AsyncClient, name: str, data: bytes, mime: str) -> httpx.Response:
    return await c.post("/api/filemod/attachments", files={"file": (name, data, mime)})


async def test_attachments_by_extension_served_as_downloads(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("filemod", manifest_extra=PERMS, body=ATT_BODY)
    async with running(database_url, tmp_path, mod) as c:
        # Outlook messages usually arrive as a generic type: the extension decides.
        r = await attach(c, "Quote.msg", b"MSG bytes", "application/octet-stream")
        assert r.status_code == 200, r.text
        d = await c.get(r.json()["url"])
        assert d.headers["content-type"].startswith("application/vnd.ms-outlook")
        assert d.headers["content-disposition"].startswith("attachment")  # never inline
        assert d.headers["x-content-type-options"] == "nosniff"

        pdf = await attach(c, "Manual.PDF", b"%PDF-1.7", "application/pdf")
        assert pdf.status_code == 200
        bad = await attach(c, "tool.exe", b"MZ", "application/octet-stream")
        assert bad.status_code == 400 and "isn't allowed" in bad.json()["detail"]
        # Screenshots still show inline.
        img = await c.post("/api/filemod/files", files={"file": ("a.png", PNG, "image/png")})
        assert (await c.get(img.json()["url"])).headers["content-disposition"].startswith("inline")


async def test_attachments_get_their_own_size_limit(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("filemod", manifest_extra=PERMS, body=ATT_BODY)
    settings = Settings(database_url_override=database_url, files_dir=str(tmp_path),
                        max_upload_mb=1, max_attachment_mb=2)  # fmt: skip
    app = create_app(settings, modules=[mod])
    big = b"x" * (1024 * 1024 + 10)  # over the image limit, under the attachment limit
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            ok = await attach(c, "big.pdf", big, "application/pdf")
            assert ok.status_code == 200
            too_big = await attach(c, "huge.pdf", big * 2, "application/pdf")
            assert too_big.status_code == 400 and "larger than 2 MB" in too_big.json()["detail"]
