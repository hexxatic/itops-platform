"""The application: discover modules, wire them to core services, serve everything.

Routes:
    /                 home (installed modules)
    /<module>/...     module pages
    /api/<module>/... module REST APIs
    /mcp/             one MCP endpoint for every module's tools (streamable HTTP)
    /files/<id>       stored files (screenshots etc.), permission-checked
    /access/...       who sees an area: AD groups, kept out of AI (admins, ADR 0012)
    /search           platform-wide search (every module the user can read)
    /auth/...         Entra sign-in (entra mode)
    /account          who you are; personal access tokens for AI tools
    /audit            the audit trail (admins)
    /connectors       data flows in and out: runs, dry runs, history (admins)
    /<module>/types   record types, for modules that use them (admins)
    /health           liveness + module load report
    /_design          component gallery
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from itops import __version__
from itops.core import auth, loader, migrations
from itops.core.account import mount_account
from itops.core.ai import AIService
from itops.core.ai_connections_page import mount_ai_connections
from itops.core.areas_page import mount_access
from itops.core.audit_page import mount_audit
from itops.core.bus import EventBus
from itops.core.config import Settings, get_settings
from itops.core.connectors_page import mount_connectors
from itops.core.content import Passage
from itops.core.db import make_engine, make_sessionmaker
from itops.core.directory import check_user as check_directory
from itops.core.directory import group_exists
from itops.core.entra import AuthGate
from itops.core.files import FileStore, is_image, may_read
from itops.core.identity import Identity, PatVerifier, principal_from_access_token
from itops.core.mcp import build_server
from itops.core.oauth import SCOPE as OAUTH_SCOPE
from itops.core.oauth import OAuthProvider
from itops.core.oauth_page import mount_oauth
from itops.core.ratelimit import Limiter, Limits, RateLimit
from itops.core.registry import ServiceRegistry
from itops.core.release import release_info, release_version
from itops.core.runtime import Runtime
from itops.core.search import SearchIndex
from itops.core.theme import AppearanceMiddleware
from itops.core.theme import discover as discover_theme
from itops.core.tools_page import mount_tools
from itops.core.types_page import mount_types
from itops.core.ui import build_templates, mount_ui
from itops.sdk import Module

log = logging.getLogger("itops")


def create_app(
    settings: Settings | None = None,
    *,
    modules: list[Module] | None = None,
) -> FastAPI:
    """Build the app. ``modules`` overrides entry-point discovery (tests)."""
    settings = settings or get_settings()
    auth.configure(settings)

    if modules is None:
        found, failed = loader.discover_installed()
    else:
        found, failed = modules, {}
    result = loader.resolve(found, failed)
    for name, problems in result.failed.items():
        log.error("module %r refused:\n  - %s", name, "\n  - ".join(problems))
    loaded = result.loaded
    auth.set_human_only({p for mod in loaded for p in mod.manifest.human_only_permissions})

    ai = AIService(settings)
    runtime = Runtime(
        settings=settings,
        bus=EventBus(),
        registry=ServiceRegistry(),
        ai=ai,
        files=FileStore(
            settings.files_dir,
            max_bytes=settings.max_upload_mb * 1024 * 1024,
            max_attachment_bytes=settings.max_attachment_mb * 1024 * 1024,
        ),
        search=SearchIndex(ai),
    )
    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    runtime.sessionmaker = sessionmaker
    runtime.connectors.sessionmaker = sessionmaker
    runtime.connectors.audit = runtime.audit
    runtime.actions.sessionmaker = sessionmaker
    runtime.jobs.sessionmaker = sessionmaker
    runtime.suggestions.sessionmaker = sessionmaker
    runtime.suggestions.files = runtime.files
    runtime.suggestions.ai = runtime.ai
    runtime.actions.audit = runtime.audit
    runtime.areas.sessionmaker = sessionmaker
    runtime.areas.audit = runtime.audit
    runtime.areas.registry = runtime.registry
    runtime.search.areas = runtime.areas
    runtime.areas.on_change.append(runtime.search.forget_vocabulary)

    for mod in loaded:
        mod.bind(runtime)
        runtime.areas.declare(mod.name, mod.manifest.areas)
        for short, fn in mod.service_fns.items():
            runtime.registry.register(f"{mod.name}.{short}", fn)
        for sub in mod.subscriptions:
            runtime.bus.subscribe(sub.event, sub.handler, subscriber=mod.name)
        for spec in mod.connectors:
            runtime.connectors.register(
                mod.name, spec.name, mod.manifest.connectors[spec.name], spec.fn
            )
        for job in mod.jobs:
            runtime.jobs.register(mod.name, job.name, mod.manifest.jobs[job.name], job.fn)
        for action in mod.actions:
            runtime.actions.register(
                mod.name, mod.manifest.title or mod.name, action.name,
                mod.manifest.actions[action.name], action.fn,
            )  # fmt: skip

    identity = Identity(sessionmaker, runtime.audit)
    identity.role_groups = dict(settings.role_groups)
    identity.watched_groups = runtime.areas.watched_groups
    runtime.people.sessionmaker = sessionmaker
    runtime.people.permissions_of = identity.permissions_of
    if settings.auth_mode == "entra" and settings.directory_check and sys.platform == "win32":
        identity.directory = check_directory
        runtime.areas.group_exists = group_exists
    identity.permission_groups = {p: list(g) for p, g in settings.permission_groups.items()}
    for problem in identity.set_declared_permissions(
        (p for mod in loaded for p in mod.manifest.permissions),
        (p for mod in loaded for p in mod.manifest.group_granted_permissions),
    ):
        log.warning("permissions: %s", problem)
    entra = settings.auth_mode == "entra"
    mcp_public = settings.mcp_public_url.rstrip("/")
    oauth = (
        OAuthProvider(
            sessionmaker,
            site_url=settings.public_url,
            redirect_hosts=settings.oauth_redirect_hosts,
            audit=runtime.audit,
            still_allowed=identity.still_allowed,
        )
        if entra and mcp_public
        else None
    )
    allowed_hosts = list(settings.allowed_hosts)
    if mcp_public:
        allowed_hosts.append(urlsplit(mcp_public).netloc)
    if entra:
        # MCP clients send a personal access token or, with ADR 0013, an OAuth access token
        # IT-Ops issued; the SDK verifies it per request and our tool wrapper reads the
        # verified identity back (auth.current()). With OAuth on, a 401 points clients at
        # the protected-resource metadata, which names IT-Ops as the authorization server.
        mcp_server = build_server(
            loaded,
            runtime,
            token_verifier=PatVerifier(identity),
            auth_settings=AuthSettings(
                issuer_url=mcp_public or settings.public_url,  # pyright: ignore[reportArgumentType]
                resource_server_url=f"{mcp_public}/mcp" if oauth else None,  # pyright: ignore[reportArgumentType]
                validate_token_resource=False if oauth else None,
            ),
        )
        auth.set_mcp_resolver(
            lambda: principal_from_access_token(t, identity) if (t := get_access_token()) else None
        )
    else:
        mcp_server = build_server(loaded, runtime)
        auth.set_mcp_resolver(None)
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"{s}://{h}" for h in allowed_hosts for s in ("http", "https")],
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        if settings.auto_migrate:
            if applied := await migrations.upgrade_core(engine):
                log.info("migrated core: %s", applied)
            for mod in loaded:
                applied = await migrations.upgrade(
                    engine, mod.name, migrations.discover(mod.package, mod.name)
                )
                if applied:
                    log.info("migrated %s: %s", mod.name, applied)
        await _seed_starter_types(runtime, loaded)
        if interrupted := await runtime.connectors.recover():
            log.warning("marked %d interrupted connector run(s) as failed", interrupted)
        if interrupted := await runtime.actions.recover():
            log.warning("marked %d interrupted tool run(s) as failed", interrupted)
        if interrupted := await runtime.jobs.recover():
            log.warning("marked %d interrupted job run(s) as failed", interrupted)
        if interrupted := await runtime.suggestions.recover():
            log.warning("marked %d interrupted AI suggestion run(s) as failed", interrupted)
        await _index_tools(runtime)
        reindexing = asyncio.create_task(_reindex_changed(runtime, loaded))
        scheduler = (
            asyncio.create_task(runtime.connectors.scheduler())
            if settings.run_connectors and runtime.connectors.all()
            else None
        )
        jobs = (
            asyncio.create_task(runtime.jobs.scheduler())
            if settings.run_jobs and runtime.jobs.all()
            else None
        )
        async with mcp_server.session_manager.run():
            yield
        for task in (scheduler, jobs, reindexing):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await runtime.bus.drain()
        await engine.dispose()

    app = FastAPI(title=settings.brand_name, version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    app.state.identity = identity
    app.state.load_result = result

    for mod in loaded:
        app.include_router(mod.router, prefix=f"/api/{mod.name}", tags=[mod.name])

    @app.get("/health", tags=["core"])
    async def health() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return {
            "status": "ok",
            # The release that's running (its tag on production), so a deploy can confirm
            # the new release is the one answering. "dev" outside a release bundle.
            "version": release_version(),
            "release": release_info(),
            "modules": {m.name: m.manifest.version for m in loaded},
            "refused": result.failed,
        }

    @app.get("/files/{file_id}", include_in_schema=False)
    async def download(file_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        """Serve a stored file after checking its owning module's read permission, and that
        the viewer may see what it belongs to (a document in a restricted collection: the
        module's ``visible_entity`` service, ADR 0012)."""
        maker = runtime.sessionmaker
        assert maker is not None
        async with maker() as session:
            row = await runtime.files.get(session, file_id)
        if row is None:
            raise HTTPException(status_code=404)
        try:
            auth.require(row.read_permission)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        visible = runtime.registry.get(f"{row.module}.visible_entity")
        if not await may_read(row, auth.current(), visible):
            raise HTTPException(status_code=404)
        return FileResponse(
            runtime.files.resolve(row.storage_path),
            media_type=row.mime_type,
            filename=row.filename,
            # Images show inline (screenshots in documents); everything else downloads,
            # never rendered inside an IT-Ops page.
            content_disposition_type="inline" if is_image(row.mime_type) else "attachment",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, max-age=86400",
            },
        )

    theme = discover_theme(settings.theme)
    templates = build_templates(loaded, settings, theme)

    @app.get("/theme/{filename}", include_in_schema=False)
    async def theme_file(filename: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        """The active theme's own files (core/theme.py); public, like /static."""
        media = theme.files().get(filename) if theme else None
        if theme is None or media is None:
            raise HTTPException(status_code=404)
        return Response(theme.read(filename), media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})  # fmt: skip

    @app.get("/search", response_class=HTMLResponse, include_in_schema=False)
    async def search_page(  # pyright: ignore[reportUnusedFunction]
        request: Request, q: str = "", archived: bool = False, exact: bool = False
    ) -> Response:
        """Platform-wide search: every module's published content the user can read."""
        maker = runtime.sessionmaker
        assert maker is not None
        async with maker() as session:
            hits = await runtime.search.query(
                session, q, principal=auth.current(), limit=30, include_archived=archived,
                fuzzy=not exact,
            )  # fmt: skip
        return templates.TemplateResponse(
            request,
            "core/search.html",
            {
                "page_title": "Search",
                "query": q,
                "hits": hits,
                "archived": archived,
                "module_titles": {
                    "itops": "Tools",
                    **{m.name: m.manifest.title or m.name for m in loaded},
                },
            },
        )

    admin_links = [
        {"label": "Audit trail", "href": "/audit", "permission": "itops.audit"},
        {"label": "Connectors", "href": "/connectors", "permission": "itops.connectors"},
        {"label": "AI connections", "href": "/ai-connections", "permission": "itops.audit"},
    ] + [
        {
            "label": f"{m.manifest.title or m.name} types",
            "href": f"/{m.name}/types",
            "permission": f"{m.name}.types.draft",
        }
        for m in loaded
        if m.manifest.uses_types
    ]
    mount_account(app, templates, settings, identity, admin_links, oauth)
    if oauth is not None:
        # ADR 0013: the OAuth endpoints and discovery documents, at the root of the public
        # MCP host (the SDK's handlers; IT-Ops' storage and Entra sign-in behind them).
        issuer = AnyHttpUrl(mcp_public)
        app.router.routes.extend(
            create_auth_routes(
                provider=oauth,
                issuer_url=issuer,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=[OAUTH_SCOPE], default_scopes=[OAUTH_SCOPE]
                ),
                revocation_options=RevocationOptions(enabled=True),
            )
            + create_protected_resource_routes(
                resource_url=AnyHttpUrl(f"{mcp_public}/mcp"),
                authorization_servers=[issuer],
                scopes_supported=[OAUTH_SCOPE],
                resource_name=settings.brand_name,
            )
        )
        mount_oauth(app, templates, settings, oauth)
    limiter = Limiter(
        Limits(
            sign_in_per_minute=settings.rate_sign_in_per_minute,
            failed_tokens_per_5_minutes=settings.rate_failed_tokens_per_5_minutes,
            token_per_minute=settings.rate_token_per_minute,
        ),
        trusted_proxies=settings.trusted_proxies,
        known=identity.token_known,
    )
    app.state.limiter = limiter
    mount_ai_connections(app, templates, identity, oauth, limiter)
    mount_access(app, templates, runtime)
    mount_audit(app, templates, runtime)
    mount_connectors(app, templates, runtime)
    mount_tools(app, templates, runtime)
    mount_types(app, templates, runtime, loaded)
    mount_ui(app, templates, loaded)
    app.mount("/mcp", mcp_app)

    # Last added runs first: sessions, then the gate that turns them into a principal, and
    # the person's light/dark choice.
    app.add_middleware(AppearanceMiddleware)
    app.add_middleware(AuthGate, settings=settings, identity=identity)
    secret = settings.secret_key.get_secret_value() or secrets.token_urlsafe(32)  # dev: throwaway
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="itops_session",
        max_age=settings.session_hours * 3600,
        same_site="lax",  # cross-site POSTs arrive without the cookie (CSRF)
        https_only=not settings.is_dev,
    )
    app.add_middleware(RateLimit, limiter=limiter)  # refuses before any work
    app.add_middleware(McpWithoutSlash)  # outermost
    return app


class McpWithoutSlash:
    """``/mcp`` is ``/mcp/``. The resource is advertised as ``.../mcp`` (ADR 0013) and some
    clients (Claude) post there; Starlette's mount would answer with a redirect, which they
    don't follow (and the reverse proxy would point it at the internal name)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


async def _reindex_changed(runtime: Runtime, modules: list[Module]) -> None:
    """Re-index each module whose ``module.search.version`` went up since its entries were
    built (once, in the background, after startup). A module never recorded counts as
    version 1."""
    if not runtime.settings.reindex_on_start:
        return
    maker = runtime.sessionmaker
    assert maker is not None
    for mod in modules:
        if not mod.search.reindexers:
            continue
        try:
            async with maker() as s:
                stored = (
                    await s.execute(
                        text("SELECT version FROM itops.search_versions WHERE module = :m"),
                        {"m": mod.name},
                    )
                ).scalar()
            if mod.search.version <= int(stored or 1):
                continue
            log.info("re-indexing %s for search version %d", mod.name, mod.search.version)
            count = 0
            for reindex in mod.search.reindexers:
                count += await reindex()
            async with maker() as s:
                await s.execute(
                    text(
                        "MERGE itops.search_versions AS t USING (SELECT :m AS module) AS src"
                        " ON t.module = src.module"
                        " WHEN MATCHED THEN UPDATE SET version = :v, rebuilt_at = sysutcdatetime()"
                        " WHEN NOT MATCHED THEN INSERT (module, version) VALUES (:m, :v);"
                    ),
                    {"m": mod.name, "v": mod.search.version},
                )
                await s.commit()
            log.info("re-indexed %s: %d entries", mod.name, count)
        except Exception:  # try again at the next start; search keeps the old entries
            log.exception("re-indexing %s failed", mod.name)


async def _index_tools(runtime: Runtime) -> None:
    """Tools are searchable ("delta sync" offers the tool), visible to people who may run
    them. Rebuilt at startup, so removed tools drop out."""
    maker = runtime.sessionmaker
    assert maker is not None
    async with maker() as s:
        await s.execute(
            text("DELETE FROM itops.search_passages WHERE module = 'itops' AND kind = 'tool'")
        )
        for a in runtime.actions.all():
            await runtime.search.replace(
                s, module="itops", kind="tool", entity_id=a.key, title=a.decl.title,
                url=f"/tools/{a.key}", read_permission=a.decl.permission,
                passages=[Passage(label=a.module_title, anchor=None,
                                  text=a.decl.description or a.decl.title)],
            )  # fmt: skip
        await s.commit()


async def _seed_starter_types(runtime: Runtime, modules: list[Module]) -> None:
    """Modules that use record types may bundle ``starter_types.json`` ([{key, name,
    description, fields}]); missing ones are created and published (ADR 0009)."""
    maker = runtime.sessionmaker
    assert maker is not None
    for mod in modules:
        if not mod.manifest.uses_types:
            continue
        res = resources.files(mod.package).joinpath("starter_types.json")
        if not res.is_file():
            continue
        starters: list[dict[str, Any]] = json.loads(res.read_text("utf-8"))
        async with maker() as s:
            added = [
                st["key"]
                for st in starters
                if await runtime.types.seed(
                    s, mod.name, st["key"], {k: v for k, v in st.items() if k != "key"}
                )
            ]
            await s.commit()
        if added:
            log.info("%s: added starter types %s", mod.name, added)
