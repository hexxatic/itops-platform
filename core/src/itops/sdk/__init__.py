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

import contextvars
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import MetaData
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from itops.core import auth
from itops.core.actions import ActionContext, ActionError, ActionFn
from itops.core.ai import LLM, AIService, AIUnavailable
from itops.core.audit import Entry as AuditEntry
from itops.core.bus import Event, EventBus
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
from itops.core.files import FileRejected, StoredFile, may_read
from itops.core.forms import KeptForm
from itops.core.manifest import Manifest
from itops.core.runtime import Runtime
from itops.core.search import Hit
from itops.core.suggest import SuggestField, Suggestion, SuggestionRun
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
    "SuggestField",
    "Suggestion",
    "SuggestionRun",
    "TypeProblem",
    "TypeView",
]

Principal = auth.Principal
PermissionDenied = auth.PermissionDenied

AsyncFn = Callable[..., Awaitable[Any]]
PageHandler = Callable[[Request], Awaitable[dict[str, Any]]]


# The session ``module.session()`` has open in this task: events published while it's open
# wait for its commit (and are dropped on rollback), so listeners never hear about a change
# that wasn't saved.
_open_session: contextvars.ContextVar[AsyncSession | None] = contextvars.ContextVar(
    "itops_open_session", default=None
)
_PENDING = "itops_pending_events"


def _hold_events(s: AsyncSession, bus: EventBus) -> None:
    """Send the session's queued events when it commits; drop them when it rolls back."""
    sync = s.sync_session
    if sync.info.get(_PENDING) is not None:
        return
    sync.info[_PENDING] = []

    def committed(_session: Any) -> None:
        pending: list[Event] = sync.info[_PENDING]
        sync.info[_PENDING] = []
        for ev in pending:
            bus.publish(ev)

    def rolled_back(_session: Any) -> None:
        sync.info[_PENDING] = []

    sa_event.listen(sync, "after_commit", committed)
    sa_event.listen(sync, "after_rollback", rolled_back)


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


JobFn = Callable[[], Awaitable[str | None]]


@dataclass(frozen=True)
class JobSpec:
    name: str  # short name; runs as "<module>.<name>"
    fn: JobFn


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

    def can_suggest(self, feature: str) -> bool:
        """Whether suggestions from files are on for ``feature`` (show the button only then)."""
        return self.llm(feature) is not None

    async def suggest(
        self,
        feature: str,
        file_id: str,
        fields: list[SuggestField],
        *,
        entity: tuple[str, str],
        instructions: str = "",
    ) -> int:
        """Have the embedded LLM read one of this module's files and suggest ``fields``
        (ADR 0015). Runs in the background, one at a time; returns the run id. Every
        suggestion it keeps is backed by a quote found in the file. Raises
        :class:`AIUnavailable` when ``feature`` isn't enabled. Check that the file belongs to
        ``entity`` and that the person may change it before calling."""
        return await self._module.runtime.suggestions.start(
            module=self._module.name, feature=feature, file_id=file_id,
            entity_type=entity[0], entity_id=entity[1], fields=fields,
            instructions=instructions,
        )  # fmt: skip

    async def suggestion_run(self, run_id: int) -> SuggestionRun | None:
        """One of this module's runs (status, suggestions with quote and page), or None."""
        run = await self._module.runtime.suggestions.get(run_id)
        return run if run is not None and run.module == self._module.name else None

    async def suggestion_runs(self, entity_type: str, entity_id: str) -> list[SuggestionRun]:
        """An entity's runs, newest first."""
        return await self._module.runtime.suggestions.for_entity(
            self._module.name, entity_type, entity_id
        )

    async def suggestions_reviewed(self, run_id: int, accepted: dict[str, Any]) -> None:
        """Record what a person accepted ({key: value saved}); audit it in your module too."""
        who = auth.current()
        await self._module.runtime.suggestions.reviewed(
            run_id, accepted=accepted, by=who.user if who else "system"
        )


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

    async def copy(self, file_id: str, *, read_permission: str | None = None) -> StoredFile:
        """Copy a file another module owns into this one (unclaimed: claim it like an upload),
        e.g. a document's attachment moving to a contract. Only if the current principal may
        read it, exactly as a download is checked; else :class:`FileRejected`."""
        rt = self._module.runtime
        principal = auth.current()
        async with self._module.session() as s:
            row = await rt.files.get(s, file_id)
            visible = rt.registry.get(f"{row.module}.visible_entity") if row else None
            if row is None or not await may_read(row, principal, visible):
                raise FileRejected(f"No file {file_id!r} you can read.")
            stored = await rt.files.copy(
                s, row, module=self._module.name,
                read_permission=self._read_permission(read_permission),
                created_by=principal.user if principal else "system",
            )  # fmt: skip
            await s.commit()
        return stored

    async def info(self, file_id: str) -> StoredFile | None:
        """One of this module's files (name, type, size, url), or None: e.g. to list an
        upload on its entity once it's saved."""
        rt = self._module.runtime
        async with self._module.session() as s:
            row = await rt.files.get(s, file_id)
        if row is None or row.module != self._module.name:
            return None
        try:
            size = rt.files.resolve(row.storage_path).stat().st_size
        except OSError:
            size = 0
        return StoredFile(id=row.id, url=f"/files/{row.id}", filename=row.filename,
                          mime_type=row.mime_type, size_bytes=size)  # fmt: skip

    async def text(self, file_id: str, *, session: AsyncSession | None = None) -> list[str]:
        """A file's text, one entry per page (sheet for Excel), for search and AI: PDF, Word,
        Excel, Outlook .msg, text and CSV. ``[]`` for anything without text (scans, images).
        Read once per file content and cached. This module's own files, or ones the current
        principal may read. Inside a transaction that touched the file (e.g. claimed it),
        pass that ``session``: a second connection would wait on its locks. The caller then
        commits."""
        if session is not None:
            return await self._text(session, file_id)
        async with self._module.session() as s:
            pages = await self._text(s, file_id)
            await s.commit()
        return pages

    async def _text(self, session: AsyncSession, file_id: str) -> list[str]:
        rt = self._module.runtime
        row = await rt.files.get(session, file_id)
        if row is None:
            return []
        if row.module != self._module.name:
            visible = rt.registry.get(f"{row.module}.visible_entity")
            if not await may_read(row, auth.current(), visible):
                return []
        return await rt.files.text(session, row)

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
        # Raise it when what this module indexes changes: IT-Ops then runs its reindexers
        # once, in the background, the next time it starts.
        self.version = 1

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
        area: tuple[str, str] | None = None,
    ) -> int:
        """Replace what's indexed for one entity. ``path`` is inside this module
        (``/d/<slug>`` becomes ``/docs/d/<slug>``). Readers need ``read_permission``.
        ``area``: (kind, key) of the area it's in (ADR 0012), so searches leave it out for
        people who can't see that area. Re-index when it moves to another area."""
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
            area=self._module.areas.id(*area) if area else None,
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
        published content (used by ``itops search rebuild``, and at startup after
        ``module.search.version`` went up)."""
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

        link_options = await module.links.options(definition, keep=values)  # ui.field_inputs
        links = await module.links.resolve(values)                          # ui.field_values

    Lookups run as the viewer: something in an area they can't see (ADR 0012) is left out
    of pickers and searches, and ``lookup(ids=...)`` returns it as ``{id, kind,
    restricted: "1"}`` (no name, no url), which ``resolve`` shows as *Restricted*.
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    def _lookup(self, target_module: str) -> Callable[..., Any] | None:
        mine = self._module.name
        if target_module != mine and target_module not in self._module.manifest.dependency_names():
            return None
        return self._module.runtime.registry.get(f"{target_module}.lookup")

    async def options(
        self,
        definition: TypeDef | None,
        *,
        limit: int = 500,
        keep: dict[str, Any] | None = None,
    ) -> dict[str, list[tuple[str, str]]]:
        """{target: [("module:kind:id", name)]} for each link field's target. ``keep``: the
        values being edited; links they hold that the picker wouldn't offer (restricted,
        archived) are added, so saving the form doesn't drop them."""
        out: dict[str, list[tuple[str, str]]] = {}
        fields = [f for f in definition.fields if f.kind == "link"] if definition else []
        for f in fields:
            target = f.target
            if not target or target in out:
                continue
            target_module, _, kind = target.partition(".")
            fn = self._lookup(target_module)
            if fn is None:
                continue
            found: list[dict[str, str]] = await fn(kind=kind or None, limit=limit)
            out[target] = [(f"{target_module}:{r['kind']}:{r['id']}", r["name"]) for r in found]
        if keep:
            labels = await self.resolve(keep)
            for f in fields:
                offered = out.get(f.target or "")
                if offered is None:
                    continue
                have = {value for value, _ in offered}
                for item in _link_items(keep.get(f.key)):
                    if item not in have:
                        offered.append((item, labels.get(item, {}).get("text", item)))
                        have.add(item)
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
                out[f"{target_module}:{r['kind']}:{r['id']}"] = (
                    {"text": "Restricted", "href": ""}
                    if r.get("restricted")
                    else {"text": r["name"], "href": r["url"]}
                )
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


def _link_items(raw: Any) -> list[str]:
    """A link field's stored value(s) as "module:kind:id" strings."""
    items: list[Any] = raw if isinstance(raw, list) else [raw]  # pyright: ignore[reportUnknownVariableType]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            d: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
            if d.get("module") and d.get("id"):
                out.append(f"{d['module']}:{d.get('kind', '')}:{d['id']}")
        elif isinstance(item, str) and item.count(":") >= 2:
            out.append(item)
    return out


class Areas:
    """Visibility of this module's areas by AD group (ADR 0012), declared in the manifest
    (``[areas] collection = "Collection"``). An admin restricts an area to AD groups and/or
    keeps it out of AI connections, on the access page core serves. The module keeps
    everything in an area the viewer can't see out of every list, count, lookup, picker and
    MCP tool, and answers 404 for its pages::

        hidden = await module.areas.hidden("collection")      # keys to leave out
        if not await module.areas.visible("collection", str(col.id)): ...
        await module.search.index(..., area=("collection", str(col.id)))

    Provide ``describe_area(kind, key) -> {name, url} | None`` (in ``provides``); the access
    page shows that name and links back to that url. Search filters by area on its own.
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    def _kind(self, kind: str) -> str:
        if kind not in self._module.manifest.areas:
            raise ValueError(
                f"{self._module.name}: area kind {kind!r} is not declared in manifest.toml [areas]"
            )
        return kind

    def id(self, kind: str, key: str) -> str:
        """The area's id, '<module>:<kind>:<key>'."""
        return f"{self._module.name}:{self._kind(kind)}:{key}"

    async def hidden(self, kind: str) -> frozenset[str]:
        """Keys of the ``kind`` areas the current principal can't see (usually none)."""
        return await self._module.runtime.areas.hidden(
            auth.current(), self._module.name, self._kind(kind)
        )

    async def visible(self, kind: str, key: str) -> bool:
        return str(key) not in await self.hidden(kind)

    async def access(self, kind: str, key: str) -> dict[str, Any]:
        """What a page shows about an area (``ui.access_summary`` / ``ui.access_badges``):
        whether it's restricted, to which groups, whether it's kept out of AI, and the
        access page's url when the viewer may change it."""
        return await self._module.runtime.areas.access(
            auth.current(), self._module.name, self._kind(kind), str(key)
        )


class People:
    """People who use IT-Ops (ADR 0014), for a record that points at a person (an owner,
    an assignee). Store the person's UPN; show their current name::

        choices = await module.people.options(holding="contracts.read")  # [(upn, name)]
        names = await module.people.names([c.owner for c in contracts])  # {upn: name}

    Only people who have signed in to IT-Ops are listed (that's when IT-Ops learns them).
    """

    def __init__(self, module: Module) -> None:
        self._module = module

    async def options(self, *, holding: str | None = None) -> list[tuple[str, str]]:
        """(upn, name) for a picker, by name; ``holding``: only people with that permission."""
        people = await self._module.runtime.people.list(holding=holding)
        return [(p.upn, p.name) for p in people]

    async def names(self, upns: Iterable[str]) -> dict[str, str]:
        """UPN -> display name, for the ones IT-Ops knows."""
        return await self._module.runtime.people.names(upns)


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
        self.jobs: list[JobSpec] = []
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
        self.areas = Areas(self)
        self.people = People(self)

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

    def job(self, name: str) -> Callable[[JobFn], JobFn]:
        """Register a scheduled job (ADR 0014), declared in the manifest as ``[jobs.<name>]``:
        ``async def run() -> str | None`` (a short summary). The core runs it on its
        schedule as itself, with the job's declared permissions; it works only on this
        module's data (publish events for anything beyond)."""

        def decorator(fn: JobFn) -> JobFn:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"{self.name}: job {name!r} must be async")
            self.jobs.append(JobSpec(name, fn))
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
        s = _open_session.get()
        if s is None:  # not inside module.session(): nothing to wait for
            self.runtime.bus.publish(ev)
        else:  # sent when that session commits, dropped if it rolls back
            s.sync_session.info[_PENDING].append(ev)
        return ev

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """A database session. Commit explicitly; rolls back on error. Events published
        while it's open are sent when it commits (dropped if it rolls back)."""
        maker = self.runtime.sessionmaker
        if maker is None:
            raise ModuleNotLoaded("database is not configured")
        async with maker() as s:
            _hold_events(s, self.runtime.bus)
            token = _open_session.set(s)
            try:
                yield s
            except BaseException:
                await s.rollback()
                raise
            finally:
                _open_session.reset(token)
