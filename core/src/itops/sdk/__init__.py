"""The IT-Ops module SDK: the only core API a module may use.

A module is a package ``itops_<name>`` that bundles a ``manifest.toml`` and exposes one
:class:`Module` object, registered under the ``itops.modules`` entry-point group::

    # src/itops_docs/__init__.py
    from itops.sdk import Module

    module = Module(__name__)

    @module.tool(permission="docs.read")
    async def search(query: str) -> list[dict[str, str]]:
        '''Search published documentation.'''
        ...

Registration (routes, tools, pages, services, subscriptions) happens at import time.
Runtime facilities (``publish``, ``session``, ``services``, ``ai``) work once the core
has loaded the module.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from itops.core import auth
from itops.core.actions import ActionContext, ActionError, ActionFn
from itops.core.ai import LLM, AIService, AIUnavailable
from itops.core.audit import Entry as AuditEntry
from itops.core.bus import Event
from itops.core.connectors import (
    ConnectorContext,
    ConnectorError,
    ConnectorFn,
    RunInfo,
    SourceRef,
    current_run,
)
from itops.core.connectors import fingerprint as _fingerprint
from itops.core.content import Passage
from itops.core.files import FileRejected, StoredFile
from itops.core.forms import KeptForm
from itops.core.manifest import Manifest
from itops.core.runtime import Runtime
from itops.core.search import Hit
from itops.core.types import TypeDef, TypeProblem, TypeView, validate_values

__all__ = [
    "LLM",
    "AIUnavailable",
    "ActionContext",
    "ActionError",
    "AuditEntry",
    "ConnectorContext",
    "ConnectorError",
    "Event",
    "FileRejected",
    "Hit",
    "KeptForm",
    "Module",
    "ModuleNotLoaded",
    "PermissionDenied",
    "Principal",
    "RunInfo",
    "SourceLink",
    "SourceRef",
    "StoredFile",
    "TypeProblem",
    "TypeView",
]

Principal = auth.Principal
PermissionDenied = auth.PermissionDenied

AsyncFn = Callable[..., Awaitable[Any]]
PageHandler = Callable[[Request], Awaitable[dict[str, Any]]]


class ModuleNotLoaded(RuntimeError):
    """A runtime facility was used before the core loaded the module."""


@dataclass(frozen=True)
class ToolSpec:
    name: str  # short name; exposed over MCP as "<module>_<name>"
    fn: AsyncFn
    description: str
    permission: str
    writes: bool


@dataclass(frozen=True)
class PageSpec:
    path: str  # relative to /<module>
    template: str  # relative to the module's templates/ folder
    title: str
    nav: str | None  # nav label, or None to keep it out of the nav
    handler: PageHandler
    permission: str | None


@dataclass(frozen=True)
class ConnectorSpec:
    name: str  # short name; runs as "<module>.<name>"
    fn: ConnectorFn


@dataclass(frozen=True)
class ActionSpec:
    name: str  # short name; runs as "<module>.<name>" from /tools
    fn: ActionFn


@dataclass(frozen=True)
class SourceLink:
    """Where a record came from, for its page: "Synced from Entra, last seen ..."."""

    connector: str
    title: str
    system: str
    last_seen: Any
    missing_since: Any
    owned: frozenset[str]


@dataclass(frozen=True)
class Subscription:
    event: str  # fully qualified "<module>.<event>"
    handler: Callable[[Event], Awaitable[None]]


class Services:
    """Typed access to other modules' services, limited to declared dependencies."""

    def __init__(self, module: Module) -> None:
        self._module = module

    def get(self, key: str) -> Callable[..., Any] | None:
        """The service ``"<module>.<service>"``, or ``None`` if that module isn't loaded."""
        target = key.split(".", 1)[0]
        if target != self._module.name and target not in self._module.manifest.dependency_names():
            raise ValueError(
                f"{self._module.name}: service {key!r} belongs to {target!r}, which is not in "
                "this module's manifest requires/optional. Declare the dependency first."
            )
        return self._module.runtime.registry.get(key)


class AI:
    """AI facilities for a module. Tier 3 (LLM) is per-feature and usually ``None``."""

    def __init__(self, module: Module) -> None:
        self._module = module

    @property
    def _svc(self) -> AIService:
        return self._module.runtime.ai

    @property
    def embeddings_available(self) -> bool:
        return self._svc.embeddings_available

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts with the local model. Raises :class:`AIUnavailable` if none."""
        return await self._svc.embed(texts)

    def llm(self, feature: str) -> LLM | None:
        """The embedded LLM for this module's ``feature``, or ``None`` (the default).

        Every caller must handle ``None``: tier 3 is optional by design.
        """
        return self._svc.llm(f"{self._module.name}.{feature}")


class Files:
    """The core file store, scoped to this module (screenshots, attachments).

    Typical flow: the editor uploads through :meth:`upload_route` while the entity may
    not exist yet; when the entity is saved, :meth:`claim` the file ids it references.
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    def _read_permission(self, permission: str | None) -> str:
        perm = permission or f"{self._module.name}.read"
        if perm not in self._module.manifest.permissions:
            raise ValueError(f"{self._module.name}: read permission {perm!r} is not declared")
        return perm

    async def save(
        self,
        data: bytes,
        *,
        mime_type: str,
        filename: str,
        read_permission: str | None = None,
        attachment: bool = False,
    ) -> StoredFile:
        """Store a file; readers need ``read_permission`` (default ``<module>.read``).
        ``attachment=True``: a document-type file (PDF, Office, Visio, ...) checked by
        extension, with the larger attachment size limit, always served as a download."""
        rt = self._module.runtime
        principal = auth.current()
        async with self._module.session() as s:
            stored = await rt.files.save(
                s,
                data=data,
                mime_type=mime_type,
                filename=filename,
                module=self._module.name,
                read_permission=self._read_permission(read_permission),
                created_by=principal.user if principal else "system",
                attachment=attachment,
            )
            await s.commit()
        return stored

    async def claim(
        self, session: AsyncSession, ids: list[str], *, entity_type: str, entity_id: str
    ) -> int:
        """Attach unclaimed uploads of this module to an entity, in the caller's session."""
        return await self._module.runtime.files.claim(
            session, module=self._module.name, ids=ids, entity_type=entity_type,
            entity_id=entity_id,
        )  # fmt: skip

    def max_bytes(self, *, attachment: bool = False) -> int:
        store = self._module.runtime.files
        return store.max_attachment_bytes if attachment else store.max_bytes

    def upload_route(
        self,
        *,
        permission: str,
        path: str = "/files",
        read_permission: str | None = None,
        attachment: bool = False,
    ) -> None:
        """Add ``POST /api/<module><path>`` (multipart ``file``) returning ``{id, url}``."""
        read_perm = self._read_permission(read_permission)
        files = self

        async def upload(file: UploadFile) -> dict[str, str]:
            try:
                auth.require(permission)
            except auth.PermissionDenied as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            data = await file.read(files.max_bytes(attachment=attachment) + 1)
            try:
                stored = await files.save(
                    data,
                    mime_type=file.content_type or "",
                    filename=file.filename or "file",
                    read_permission=read_perm,
                    attachment=attachment,
                )
            except FileRejected as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"id": stored.id, "url": stored.url}

        self._module.router.add_api_route(path, upload, methods=["POST"])


Reindexer = Callable[[], Awaitable[int]]


class Search:
    """The core search index, from this module's side (ADR 0003).

    Index **published** content only (pass ``archived=True`` for archived content: it's
    hidden unless a search asks for it); take it out when it's deleted::

        await module.search.index(s, kind="document", entity_id=str(doc.id),
                                  title=doc.title, path=f"/d/{doc.slug}",
                                  passages=passages(content))
    """

    def __init__(self, module: Module) -> None:
        self._module = module
        self.reindexers: list[Reindexer] = []

    def _perm(self, permission: str | None) -> str:
        perm = permission or f"{self._module.name}.read"
        if perm not in self._module.manifest.permissions:
            raise ValueError(f"{self._module.name}: read permission {perm!r} is not declared")
        return perm

    async def index(
        self,
        session: AsyncSession,
        *,
        kind: str,
        entity_id: str,
        title: str,
        path: str,
        passages: Iterable[Passage],
        read_permission: str | None = None,
        archived: bool = False,
    ) -> int:
        """Replace what's indexed for one entity. ``path`` is inside this module
        (``/d/<slug>`` becomes ``/docs/d/<slug>``). Readers need ``read_permission``."""
        return await self._module.runtime.search.replace(
            session,
            module=self._module.name,
            kind=kind,
            entity_id=entity_id,
            title=title,
            url=f"/{self._module.name}{path}",
            read_permission=self._perm(read_permission),
            passages=passages,
            archived=archived,
        )

    async def remove(self, session: AsyncSession, *, kind: str, entity_id: str) -> None:
        await self._module.runtime.search.remove(
            session, module=self._module.name, kind=kind, entity_id=entity_id
        )

    async def query(
        self,
        q: str,
        *,
        scope: Literal["module", "all"] = "module",
        limit: int = 20,
        include_archived: bool = False,
        fuzzy: bool = True,
    ) -> list[Hit]:
        """Search as the current user: this module only, or the whole platform.
        Archived content is left out unless ``include_archived``. Typos are corrected
        against IT-Ops' own vocabulary unless ``fuzzy=False`` (hits only the corrected
        query found carry ``corrected``)."""
        async with self._module.session() as s:
            return await self._module.runtime.search.query(
                s,
                q,
                principal=auth.current(),
                module=self._module.name if scope == "module" else None,
                limit=limit,
                include_archived=include_archived,
                fuzzy=fuzzy,
            )

    def reindexer(self, fn: Reindexer) -> Reindexer:
        """Register ``async def fn() -> int`` that re-indexes all of this module's
        published content (used by ``itops search rebuild``)."""
        self.reindexers.append(fn)
        return fn


class Audit:
    """The audit trail, from this module's side (ADR 0006). Record every change people
    care about, in the same session as the change::

        await module.audit.record(s, "document.published", kind="document",
                                  entity_id=str(doc.id), title=doc.title,
                                  summary=f"Published version {v.version_number}")

    Who did it (and whether via AI) is filled in by the core from the request.
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    async def record(
        self,
        session: AsyncSession,
        action: str,
        *,
        kind: str | None = None,
        entity_id: str | None = None,
        title: str | None = None,
        summary: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._module.runtime.audit.record(
            session,
            module=self._module.name,
            action=action,
            entity_kind=kind,
            entity_id=entity_id,
            title=title,
            summary=summary,
            details=details,
        )

    async def history(self, *, kind: str, entity_id: str, limit: int = 50) -> list[AuditEntry]:
        """One entity's entries, newest first (show them with ``ui.activity``)."""
        async with self._module.session() as s:
            return await self._module.runtime.audit.history(
                s, module=self._module.name, entity_kind=kind, entity_id=entity_id, limit=limit
            )


class Forms:
    """Keep what someone typed when a save fails, so the form comes back filled in.

    In an action (a POST that redirects back to a page)::

        except DocsError as exc:
            return await module.forms.back(request, f"/docs/d/{slug}/edit", str(exc))

    and in the page it redirects to::

        kept = module.forms.recall(request)   # KeptForm | None
        title = kept.get("title", doc.title) if kept else doc.title
        values = kept.fields() if kept else stored_values

    Uploaded files can't be kept: ``kept.files_dropped`` says some were chosen.
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    async def back(self, request: Request, url: str, error: str | None = None) -> RedirectResponse:
        """Redirect to ``url`` with ``?error=`` and, when there's a submitted form, ``kept=``."""
        params: dict[str, str] = {}
        if error:
            params["error"] = error
        principal = auth.current()
        if principal is not None and request.method == "POST":
            form = await request.form()
            values: dict[str, list[str]] = {}
            dropped = False
            for name, value in form.multi_items():
                if isinstance(value, str):
                    values.setdefault(name, []).append(value)
                elif value.filename:
                    dropped = True
            token = self._module.runtime.forms.keep(principal.user, KeptForm(values, dropped))
            if token:
                params["kept"] = token
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, quote_via=quote)
        return RedirectResponse(url, status_code=303)

    def recall(self, request: Request) -> KeptForm | None:
        """The form kept for this page's ``?kept=`` token (only the submitter sees it)."""
        principal = auth.current()
        if principal is None:
            return None
        return self._module.runtime.forms.recall(principal.user, request.query_params.get("kept"))


class Links:
    """Pickers and labels for record-type link fields (ADR 0008), across modules.

    A module that can be linked to provides ``lookup`` (``lookup(ids=[...])`` or
    ``lookup(kind=..., query=..., limit=...)`` -> ``[{id, kind, name, url}]``), plus
    ``link_targets`` for the type editor. Links reach this module and the modules it
    declares in requires/optional; a target module that isn't loaded just has no picker::

        link_options = await module.links.options(definition)   # for ui.field_inputs
        links = await module.links.resolve(values)               # for ui.field_values
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    def _lookup(self, target_module: str) -> Callable[..., Any] | None:
        mine = self._module.name
        if target_module != mine and target_module not in self._module.manifest.dependency_names():
            return None
        return self._module.runtime.registry.get(f"{target_module}.lookup")

    async def options(
        self, definition: TypeDef | None, *, limit: int = 500
    ) -> dict[str, list[tuple[str, str]]]:
        """{target: [("module:kind:id", name)]} for each link field's target."""
        out: dict[str, list[tuple[str, str]]] = {}
        for f in definition.fields if definition else []:
            target = f.target if f.kind == "link" else None
            if not target or target in out:
                continue
            target_module, _, kind = target.partition(".")
            fn = self._lookup(target_module)
            if fn is None:
                continue
            found: list[dict[str, str]] = await fn(kind=kind or None, limit=limit)
            out[target] = [(f"{target_module}:{r['kind']}:{r['id']}", r["name"]) for r in found]
        return out

    async def resolve(self, *value_sets: dict[str, Any]) -> dict[str, dict[str, str]]:
        """{"module:kind:id": {"text": name, "href": url}} for every link in these values."""
        wanted: dict[str, list[str]] = {}
        for values in value_sets:
            for raw in values.values():
                items: list[Any] = raw if isinstance(raw, list) else [raw]  # pyright: ignore[reportUnknownVariableType]
                for item in items:
                    if isinstance(item, dict) and item.get("module") and item.get("id"):  # pyright: ignore[reportUnknownMemberType]
                        d: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
                        wanted.setdefault(str(d["module"]), []).append(str(d["id"]))
        out: dict[str, dict[str, str]] = {}
        for target_module, ids in wanted.items():
            fn = self._lookup(target_module)
            if fn is None:
                continue
            found: list[dict[str, str]] = await fn(ids=list(dict.fromkeys(ids)))
            for r in found:
                out[f"{target_module}:{r['kind']}:{r['id']}"] = {
                    "text": r["name"],
                    "href": r["url"],
                }
        return out

    async def related(self, link: str) -> list[dict[str, str]]:
        """Records in this module's dependencies that link to ``link`` ("module:kind:id"):
        every declared module's ``related`` service, combined. Each: {id, kind, name, url,
        field, module}."""
        out: list[dict[str, str]] = []
        for target_module in sorted(self._module.manifest.dependency_names() - {"core"}):
            fn = self._module.runtime.registry.get(f"{target_module}.related")
            if fn is not None:
                out += [{**r, "module": target_module} for r in await fn(link=link)]
        return out


class Refs:
    """Source references (ADR 0010), from the side of a module that accepts synced data.

    A module's upsert service (e.g. ``assets.upsert``) uses these while a connector runs::

        run = module.refs.run                       # RunInfo, or None outside a connector
        ref = await module.refs.find(s, "configuration", "123")
        if ref and ref.fingerprint == module.refs.fingerprint(values): ...  # unchanged
        await module.refs.link(s, "configuration", "123", kind="device", entity_id=str(rec.id),
                               fingerprint=module.refs.fingerprint(values))

    **During a dry run (``run.dry_run``) an upsert service must not write anything.**
    Pages use ``links(kind, id)`` to show "Synced from ..." and ``owned_fields(kind, id)``
    to make the fields a connector owns read-only.
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    @property
    def run(self) -> RunInfo | None:
        """The connector run calling in, if any (its key, run id, dry run, what it owns)."""
        return current_run()

    def _require_run(self) -> RunInfo:
        run = current_run()
        if run is None:
            raise ConnectorError("only a connector run can record source references")
        return run

    @staticmethod
    def fingerprint(values: Any) -> str:
        return _fingerprint(values)

    def owns(self, kind: str) -> frozenset[str] | None:
        """Fields the running connector owns on this module's ``kind`` (``None`` outside a
        run; ``{"*"}`` means all of them)."""
        run = current_run()
        return None if run is None else run.owns.get(f"{self._module.name}.{kind}", frozenset())

    async def find(
        self, session: AsyncSession, source_type: str, source_id: str
    ) -> SourceRef | None:
        run = self._require_run()
        return await self._module.runtime.refs.find(
            session, connector=run.connector, source_type=source_type, source_id=str(source_id),
            module=self._module.name,
        )  # fmt: skip

    async def link(
        self,
        session: AsyncSession,
        source_type: str,
        source_id: str,
        *,
        kind: str,
        entity_id: str,
        fingerprint: str | None,
    ) -> None:
        run = self._require_run()
        if run.dry_run:
            raise ConnectorError("a dry run must not write (check module.refs.run.dry_run)")
        await self._module.runtime.refs.link(
            session, connector=run.connector, source_type=source_type, source_id=str(source_id),
            module=self._module.name, entity_kind=kind, entity_id=entity_id,
            fingerprint=fingerprint, run_id=run.run_id,
        )  # fmt: skip

    async def seen(self, session: AsyncSession, ref: SourceRef, *, fingerprint: str | None) -> None:
        run = self._require_run()
        if run.dry_run:
            raise ConnectorError("a dry run must not write (check module.refs.run.dry_run)")
        await self._module.runtime.refs.seen(
            session, ref, fingerprint=fingerprint, run_id=run.run_id
        )

    async def links(self, kind: str, entity_id: str) -> list[SourceLink]:
        """The connectors this record is synced from."""
        runtime = self._module.runtime
        async with self._module.session() as s:
            refs = await runtime.refs.for_entity(
                s, module=self._module.name, entity_kind=kind, entity_id=str(entity_id)
            )
        out: list[SourceLink] = []
        for r in refs:
            reg = runtime.connectors.get(r.connector)
            out.append(
                SourceLink(
                    connector=r.connector,
                    title=reg.decl.title if reg else r.connector,
                    system=reg.decl.system if reg else r.connector,
                    last_seen=r.last_seen,
                    missing_since=r.missing_since,
                    owned=runtime.connectors.owned_fields(
                        r.connector, f"{self._module.name}.{kind}"
                    ),
                )
            )
        return out

    async def owned_fields(self, kind: str, entity_id: str) -> frozenset[str]:
        """Fields of this record that a connector owns (read-only for people and AI)."""
        owned: set[str] = set()
        for link in await self.links(kind, entity_id):
            owned |= link.owned
        return frozenset(owned)


class Types:
    """Record types for this module (ADR 0008): admin-defined fields without code. Opt in by
    declaring ``<module>.types.draft`` and ``<module>.types.approve`` (human-only).

    Store your records' values yourself (versioned with your content) and validate them::

        values = await module.types.validate("vendor", form_values, require=False)

    Drafting and publishing types is done by the core pages and MCP tools; modules mostly
    read types (``list``/``get``) and validate values.
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    def _check(self) -> str:
        if not self._module.manifest.uses_types:
            raise ValueError(
                f"{self._module.name}: declare {self._module.name}.types.draft and "
                f"{self._module.name}.types.approve in manifest.toml to use record types"
            )
        return self._module.name

    async def list(self, *, include_drafts: bool = False) -> list[TypeView]:
        """Published, not-retired types (what new records can use); drafts on request."""
        name = self._check()
        async with self._module.session() as s:
            return await self._module.runtime.types.list_types(
                s, name, include_drafts=include_drafts
            )

    async def get(self, key: str) -> TypeView | None:
        name = self._check()
        async with self._module.session() as s:
            return await self._module.runtime.types.get(s, name, key)

    async def validate(
        self, key: str, values: dict[str, Any], *, require: bool = True
    ) -> dict[str, Any]:
        """Normalized values for the published type ``key``; raises ``TypeProblem`` (a
        ValueError) with a readable message. ``require=False`` for drafts."""
        name = self._check()
        async with self._module.session() as s:
            definition = await self._module.runtime.types.definition(s, name, key)
        return validate_values(definition, values, require=require)


class Module:
    def __init__(self, package: str, *, manifest: Manifest | None = None) -> None:
        self.package = package
        self.manifest = manifest or Manifest.load(package)
        self.name = self.manifest.name
        self.router = APIRouter()
        self.tools: list[ToolSpec] = []
        self.pages: list[PageSpec] = []
        self.subscriptions: list[Subscription] = []
        self.connectors: list[ConnectorSpec] = []
        self.actions: list[ActionSpec] = []
        self.service_fns: dict[str, Callable[..., Any]] = {}
        self._runtime: Runtime | None = None
        self.services = Services(self)
        self.ai = AI(self)
        self.files = Files(self)
        self.search = Search(self)
        self.audit = Audit(self)
        self.types = Types(self)
        self.forms = Forms(self)
        self.links = Links(self)
        self.refs = Refs(self)

        schema = self.name

        class Base(DeclarativeBase):
            """Declarative base whose tables live in this module's own schema."""

            metadata = MetaData(schema=schema)

        self.Base = Base

    # ---- registration (import time) -------------------------------------------------

    def tool(
        self,
        *,
        permission: str,
        name: str | None = None,
        description: str | None = None,
        writes: bool = False,
    ) -> Callable[[AsyncFn], AsyncFn]:
        """Expose an async function as an MCP tool, namespaced ``<module>_<name>``.

        The description defaults to the docstring. ``writes=True`` marks tools that
        change data; AI-initiated writes must land as drafts for human review.
        """

        def decorator(fn: AsyncFn) -> AsyncFn:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"{self.name}: tool {fn.__name__!r} must be async")
            desc = (description or inspect.getdoc(fn) or "").strip()
            self.tools.append(ToolSpec(name or fn.__name__, fn, desc, permission, writes))
            return fn

        return decorator

    def page(
        self,
        path: str,
        *,
        template: str,
        title: str,
        nav: str | None = None,
        permission: str | None = None,
    ) -> Callable[[PageHandler], PageHandler]:
        """Register a server-rendered page at ``/<module><path>``.

        The handler returns the template context. Templates extend the core shell
        (``{% extends "core/shell.html" %}``) and use only core components.
        """

        def decorator(fn: PageHandler) -> PageHandler:
            self.pages.append(PageSpec(path, template, title, nav, fn, permission))
            return fn

        return decorator

    def subscribe(self, event: str) -> Callable[[Callable[[Event], Awaitable[None]]], Any]:
        """Handle an event (``"<module>.<event>"``) published by any module."""

        def decorator(fn: Callable[[Event], Awaitable[None]]) -> Any:
            self.subscriptions.append(Subscription(event, fn))
            return fn

        return decorator

    def action(self, name: str) -> Callable[[ActionFn], ActionFn]:
        """Register a tool action (ADR 0011), declared in the manifest as
        ``[actions.<name>]``: ``async def run(ctx: ActionContext) -> None``. People run it
        from ``/tools`` (AI may request a run); it reports steps, counts, problems and log
        lines through ``ctx`` and reaches other servers with ``ctx.powershell(...)``."""

        def decorator(fn: ActionFn) -> ActionFn:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"{self.name}: action {name!r} must be async")
            self.actions.append(ActionSpec(name, fn))
            return fn

        return decorator

    def connector(self, name: str) -> Callable[[ConnectorFn], ConnectorFn]:
        """Register a connector (ADR 0010), declared in the manifest as ``[connectors.<name>]``.
        ``async def run(ctx: ConnectorContext) -> None`` reads the source and writes through
        modules' upsert services; the core runs it on schedule or from ``/connectors``."""

        def decorator(fn: ConnectorFn) -> ConnectorFn:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"{self.name}: connector {name!r} must be async")
            self.connectors.append(ConnectorSpec(name, fn))
            return fn

        return decorator

    def service(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Offer a function to other modules as ``"<module>.<name>"`` (declare in ``provides``)."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.service_fns[name] = fn
            return fn

        return decorator

    def require(self, permission: str) -> Callable[[], Principal]:
        """FastAPI dependency: ``Depends(module.require("docs.write"))``."""

        def dependency() -> Principal:
            try:
                return auth.require(permission)
            except auth.PermissionDenied as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

        return dependency

    # ---- runtime (after load) -------------------------------------------------------

    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            raise ModuleNotLoaded(f"module {self.name!r} has not been loaded by the core")
        return self._runtime

    def bind(self, runtime: Runtime) -> None:
        """Called by the core loader. Not for module code."""
        self._runtime = runtime

    def url(self, path: str = "/") -> str:
        """Absolute browser URL for a path in this module: ``module.url("/d/x")``."""
        return self.absolute_url(f"/{self.name}{path if path.startswith('/') else '/' + path}")

    def absolute_url(self, site_path: str) -> str:
        """Absolute browser URL for a site path, e.g. a search hit's ``url``."""
        return self.runtime.settings.public_url.rstrip("/") + site_path

    def current_principal(self) -> Principal | None:
        return auth.current()

    def publish(self, event: str, payload: dict[str, Any]) -> Event:
        """Publish ``"<module>.<event>"``. The event must be declared in the manifest."""
        if event not in self.manifest.events:
            raise ValueError(
                f"{self.name}: event {event!r} is not declared in manifest.toml [events]"
            )
        ev = Event(name=f"{self.name}.{event}", payload=payload, source=self.name)
        self.runtime.bus.publish(ev)
        return ev

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """A database session. Commit explicitly; rolls back on error."""
        maker = self.runtime.sessionmaker
        if maker is None:
            raise ModuleNotLoaded("database is not configured")
        async with maker() as s:
            try:
                yield s
            except BaseException:
                await s.rollback()
                raise
