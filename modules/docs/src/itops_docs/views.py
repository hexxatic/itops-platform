"""Docs pages (server-rendered, core components only) and the form actions behind them.

Pages live at /docs/...; actions are POSTs under /api/docs/... that redirect back to a
page (plain forms, no JavaScript needed). Problems come back as ``?error=...`` and are
shown as an alert on the page.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from itops.sdk import FileRejected, KeptForm, Principal
from itops.sdk.content import Content, render, toc
from itops.sdk.types import TypeDef
from itops_docs import service
from itops_docs.app import module
from itops_docs.models import Collection, Document, Version
from itops_docs.service import DocsError, known_fields, version_attachments, version_fields

module.files.upload_route(permission="docs.write")

CanRead = Annotated[Principal, Depends(module.require("docs.read"))]
CanWrite = Annotated[Principal, Depends(module.require("docs.write"))]
CanPublish = Annotated[Principal, Depends(module.require("docs.publish"))]

STATUS_TONE = {"draft": "warning", "published": "success", "archived": "neutral"}


def _when(value: dt.datetime | dt.date | None) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return value.isoformat()


def _doc_row(d: Document, type_names: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "title": {"text": d.title, "href": f"/docs/d/{d.slug}"},
        "type": (type_names or {}).get(d.type_key or "", ""),
        "collection": {"text": d.collection.name, "href": f"/docs/c/{d.collection.slug}"},
        "status": {"badge": d.status.capitalize(), "tone": STATUS_TONE[d.status]},
        "updated": _when(d.updated_at),
    }


def _back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


async def _doc_or_404(s: Any, slug: str) -> Document:
    doc = await service.by_slug(s, slug)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


async def _collection_options(s: Any) -> list[tuple[int, str]]:
    return [(c.id, c.name) for c, _ in await service.collections(s)]


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


# ---- document types (ADR 0008) ---------------------------------------------------------


async def _type_names() -> dict[str, str]:
    """Every type's current name (retired and draft-only included, for labels)."""
    return {v.key: v.current.definition.name for v in await module.types.list(include_drafts=True)}


async def _definition(type_key: str | None) -> TypeDef | None:
    if not type_key:
        return None
    try:
        return await service.type_definition(type_key)
    except DocsError:
        return None


Uploads = Annotated[list[UploadFile] | None, File()]


async def _store_uploads(files: list[UploadFile] | None) -> list[dict[str, Any]]:
    """Save uploaded attachments in the core file store (claimed when the version saves)."""
    out: list[dict[str, Any]] = []
    limit = module.files.max_bytes(attachment=True)
    for f in files or []:
        if not f.filename:
            continue  # an empty file input still sends a part
        data = await f.read(limit + 1)
        try:
            stored = await module.files.save(
                data, mime_type=f.content_type or "", filename=f.filename, attachment=True
            )
        except FileRejected as exc:
            raise DocsError(str(exc)) from exc
        out.append({"id": stored.id, "filename": stored.filename, "size": stored.size_bytes,
                    "mime": stored.mime_type})  # fmt: skip
    return out


def _kept_content(kept: KeptForm | None, fallback: Content) -> Content:
    """The editor content from a form kept after a failed save, else ``fallback``."""
    if kept is None or "content" not in kept:
        return fallback
    try:
        return Content.model_validate_json(kept.get("content") or "{}")
    except ValidationError:
        return fallback


async def _form_fields(request: Request) -> dict[str, Any]:
    """Field inputs (named f__<key>) from a submitted form; repeated names = several values."""
    form = await request.form()
    out: dict[str, Any] = {}
    for key in {k for k in form if k.startswith("f__")}:
        values = [str(v) for v in form.getlist(key) if str(v).strip()]
        out[key[3:]] = values if len(values) > 1 else (values[0] if values else "")
    return out


# ---- pages --------------------------------------------------------------------------


@module.page("/", template="index.html", title="Docs", nav="Docs", permission="docs.read")
async def home(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        cols = await service.collections(s)
        drafts = await service.documents(s, status="draft", limit=20)
        recent = await service.documents(s, status="published", limit=15)
    names = await _type_names()
    return {
        "collections": [
            {
                "name": {"text": c.name, "href": f"/docs/c/{c.slug}"},
                "description": c.description,
                "count": n,
            }
            for c, n in cols
        ],
        "drafts": [_doc_row(d, names) for d in drafts],
        "recent": [_doc_row(d, names) for d in recent],
        "can_write": _can("docs.write"),
        "can_manage_types": _can("docs.types.draft"),
        "can_manage_collections": _can("docs.collections.manage"),
        "error": request.query_params.get("error"),
    }


@module.page("/search", template="search.html", title="Search docs", permission="docs.read")
async def search(request: Request) -> dict[str, Any]:
    q = request.query_params.get("q", "")
    archived = request.query_params.get("archived") == "1"
    exact = request.query_params.get("exact") == "1"
    hits = await module.search.query(
        q, scope="module", limit=30, include_archived=archived, fuzzy=not exact
    )
    return {"query": q, "hits": hits, "archived": archived}


@module.page("/c/{slug}", template="collection.html", title="Collection", permission="docs.read")
async def collection(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        col = await service.collection_by_slug(s, request.path_params["slug"])
        if col is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        show_archived = request.query_params.get("archived") == "1"
        docs = await service.documents(s, collection_id=col.id, include_archived=show_archived)
    names = await _type_names()
    return {
        "page_title": col.name,
        "collection": col,
        "show_archived": show_archived,
        "rows": [_doc_row(d, names) for d in docs],
        "can_write": _can("docs.write"),
    }


@module.page("/d/{slug}", template="document.html", title="Document", permission="docs.read")
async def document(request: Request) -> dict[str, Any]:
    show_draft = request.query_params.get("draft") == "1"
    async with module.session() as s:
        doc = await _doc_or_404(s, request.path_params["slug"])
        v = await service.view(s, doc)
        doc_tags = await service.tags(s, doc.id)
        history = await service.versions(s, doc.id)
    activity = await module.audit.history(kind="document", entity_id=str(doc.id))
    showing_draft = v.published is None or (show_draft and v.has_pending_draft)
    content = v.content(prefer_draft=showing_draft)
    definition = await _definition(doc.type_key)
    shown = (v.draft or v.published) if showing_draft else (v.published or v.draft)
    values = known_fields(definition, version_fields(shown)) if definition else {}
    links = await module.links.resolve(values)
    related_assets = [
        {"text": r["name"], "href": r["url"], "note": r["field"]}
        for r in await module.links.related(f"docs:document:{doc.id}")
        if r["module"] == "assets"
    ]
    return {
        "page_title": doc.title,
        "doc": doc,
        "collection": doc.collection,
        "status_tone": STATUS_TONE[doc.status],
        "tags": doc_tags,
        "toc": toc(content),
        "blocks": render(content),
        "showing_draft": showing_draft,
        "pending_draft": v.has_pending_draft and v.published is not None,
        "history": [
            {
                "version": f"v{h.version_number}",
                "note": h.change_note,
                "author": h.author_user + (" (via AI)" if h.author_via_ai else ""),
                "when": _when(h.created_at),
                "state": _version_state(h, doc),
            }
            for h in history
        ],
        "activity": activity,
        "attachments": version_attachments(shown),
        "type_name": (await _type_names()).get(doc.type_key or "", doc.type_key),
        "definition": definition,
        "values": values,
        "links": links,
        "related_assets": related_assets,
        "next_review": _when(doc.next_review_at),
        "can_write": _can("docs.write") and doc.status != "archived",
        "can_publish": _can("docs.publish"),
        "error": request.query_params.get("error"),
    }


def _version_state(v: Version, doc: Document) -> dict[str, str]:
    if v.id == doc.current_version_id:
        return {"badge": "Published", "tone": "success"}
    if v.published_at is None:
        return {"badge": "Draft", "tone": "warning"}
    return {"badge": "Previous", "tone": "neutral"}


@module.page("/new", template="edit.html", title="New document", permission="docs.write")
async def new(request: Request) -> dict[str, Any]:
    preselect = request.query_params.get("collection")
    type_key = request.query_params.get("type") or None
    types = await service.available_types()
    if type_key not in {t.key for t in types}:
        type_key = None
    definition = await _definition(type_key)
    async with module.session() as s:
        options = await _collection_options(s)
        link_options = await module.links.options(definition)
    selected = preselect or (options[0][0] if options else None)
    kept = module.forms.recall(request)
    return {
        "mode": "new",
        "collections": options,
        "selected": kept.get("collection_id", str(selected)) if kept else selected,
        "title": kept.get("title") if kept else "",
        "tags": kept.get("tags") if kept else "",
        "files_dropped": bool(kept and kept.files_dropped),
        "content": _kept_content(kept, Content()).model_dump(),
        "type_key": type_key,
        "type_choices": [
            {
                "label": label,
                "href": "/docs/new?"
                + urlencode({"collection": selected or "", **({"type": key} if key else {})}),
                "current": key == type_key,
            }
            for key, label in [
                (None, "Plain document"),
                *((t.key, t.current.definition.name) for t in types),
            ]
        ],
        "definition": definition,
        "values": kept.fields() if kept else {},
        "link_options": link_options,
        "attachments": [],
        "max_attachment_mb": module.files.max_bytes(attachment=True) // (1024 * 1024),
        "error": request.query_params.get("error"),
    }


@module.page("/d/{slug}/edit", template="edit.html", title="Edit", permission="docs.write")
async def edit(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        doc = await _doc_or_404(s, request.path_params["slug"])
        v = await service.view(s, doc)
        options = await _collection_options(s)
        doc_tags = await service.tags(s, doc.id)
        definition = await _definition(doc.type_key)
        link_options = await module.links.options(definition)
    working = v.draft or v.published
    # A failed save keeps the editor form (it has "content"); a failed details change
    # keeps the details form (it has "title").
    kept = module.forms.recall(request)
    kept_save = kept if kept and "content" in kept else None
    kept_details = kept if kept and "title" in kept else None
    types = await service.available_types()
    type_options = [("", "Plain document (no type)")] + [
        (t.key, t.current.definition.name) for t in types
    ]
    if doc.type_key and doc.type_key not in {t.key for t in types}:
        type_options.append((doc.type_key, f"{doc.type_key} (retired)"))
    return {
        "page_title": f"Edit: {doc.title}",
        "definition": definition,
        "values": (
            kept_save.fields()
            if kept_save
            else known_fields(definition, version_fields(working))
            if definition
            else {}
        ),
        "link_options": link_options,
        "attachments": version_attachments(working),
        "files_dropped": bool(kept_save and kept_save.files_dropped),
        "change_note": kept_save.get("change_note") if kept_save else "",
        "title": kept_details.get("title") if kept_details else doc.title,
        "max_attachment_mb": module.files.max_bytes(attachment=True) // (1024 * 1024),
        "type_options": type_options,
        "mode": "edit",
        "doc": doc,
        "collections": options,
        "selected": kept_details.get("collection_id") if kept_details else doc.collection_id,
        "tags": kept_details.get("tags") if kept_details else ", ".join(doc_tags),
        "type_selected": kept_details.get("type_key") if kept_details else doc.type_key or "",
        "content": _kept_content(kept_save, v.content(prefer_draft=True)).model_dump(),
        "error": request.query_params.get("error"),
    }


# ---- actions ------------------------------------------------------------------------


def _parse_content(raw: str) -> Content:
    try:
        return Content.model_validate_json(raw or "{}")
    except ValidationError as exc:
        raise DocsError("The editor sent content the server couldn't read.") from exc


def _split_tags(raw: str) -> list[str]:
    return [t for t in raw.split(",") if t.strip()]


@module.router.post("/documents")
async def create_document(
    request: Request,
    who: CanWrite,
    title: Annotated[str, Form()],
    collection_id: Annotated[int, Form()],
    content: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    type_key: Annotated[str, Form()] = "",
    attachments: Uploads = None,
) -> RedirectResponse:
    fields = await _form_fields(request) if type_key else None
    try:
        uploaded = await _store_uploads(attachments)
        async with module.session() as s:
            doc = await service.create(
                s, who, collection_id=collection_id, title=title,
                content=_parse_content(content), tags=_split_tags(tags),
                type_key=type_key or None, fields=fields, attachments=uploaded,
            )  # fmt: skip
            await s.commit()
            slug = doc.slug
    except DocsError as exc:
        query = urlencode({"collection": collection_id, **({"type": type_key} if type_key else {})})
        return await module.forms.back(request, f"/docs/new?{query}", str(exc))
    return _back(f"/docs/d/{slug}?draft=1")


@module.router.post("/documents/{slug}/save")
async def save_document(
    request: Request,
    slug: str,
    who: CanWrite,
    content: Annotated[str, Form()] = "",
    change_note: Annotated[str, Form()] = "",
    attachments: Uploads = None,
    remove_attachment: Annotated[list[str] | None, Form()] = None,
) -> RedirectResponse:
    try:
        uploaded = await _store_uploads(attachments)
        async with module.session() as s:
            doc = await _doc_or_404(s, slug)
            # The editor shows the type's inputs, so a typed document's form carries them.
            fields = await _form_fields(request) if doc.type_key else None
            v = await service.view(s, doc)
            removed = set(remove_attachment or [])
            kept = [
                a for a in version_attachments(v.draft or v.published) if a["id"] not in removed
            ]
            await service.save(
                s, who, doc, _parse_content(content), note=change_note, fields=fields,
                attachments=kept + uploaded,
            )  # fmt: skip
            await s.commit()
    except DocsError as exc:
        return await module.forms.back(request, f"/docs/d/{slug}/edit", str(exc))
    return _back(f"/docs/d/{slug}?draft=1")


@module.router.post("/documents/{slug}/details")
async def update_details(
    request: Request,
    slug: str,
    who: CanWrite,
    title: Annotated[str, Form()],
    collection_id: Annotated[int, Form()],
    tags: Annotated[str, Form()] = "",
    type_key: Annotated[str, Form()] = "",
    # Sent with the type picker: an empty type then means "no type", not "unchanged"
    # (an empty optional form value can't tell the two apart).
    change_type: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    try:
        async with module.session() as s:
            doc = await _doc_or_404(s, slug)
            await service.update_details(
                s, doc, title=title, collection_id=collection_id, tags=_split_tags(tags),
                type_key=type_key or None, change_type=change_type, who=who,
            )  # fmt: skip
            await s.commit()
            slug = doc.slug  # a new title can mean a new slug
    except DocsError as exc:
        return await module.forms.back(request, f"/docs/d/{slug}/edit", str(exc))
    return _back(f"/docs/d/{slug}")


async def _lifecycle(slug: str, who: Principal, action: str) -> RedirectResponse:
    try:
        async with module.session() as s:
            doc = await _doc_or_404(s, slug)
            if action == "publish":
                await service.publish(s, who, doc)
            elif action == "archive":
                await service.archive(s, who, doc)
            elif action == "restore":
                await service.restore(s, who, doc)
            elif action == "discard":
                outcome = await service.discard(s, who, doc)
                await s.commit()
                return _back("/docs/" if outcome == "deleted" else f"/docs/d/{slug}")
            await s.commit()
    except DocsError as exc:
        return _back(f"/docs/d/{slug}", str(exc))
    return _back(f"/docs/d/{slug}")


@module.router.post("/documents/{slug}/publish")
async def publish_document(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "publish")


@module.router.post("/documents/{slug}/discard")
async def discard_draft(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "discard")


@module.router.post("/documents/{slug}/archive")
async def archive_document(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "archive")


@module.router.post("/documents/{slug}/restore")
async def restore_document(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "restore")


# ---- collections (admins) -------------------------------------------------------------------

CanManageCollections = Annotated[Principal, Depends(module.require("docs.collections.manage"))]


async def _collection_or_404(s: Any, slug: str) -> Collection:
    col = await service.collection_by_slug(s, slug)
    if col is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return col


@module.page(
    "/collections",
    template="collections.html",
    title="Manage collections",
    permission="docs.collections.manage",
)
async def manage_collections(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        cols = [c for c, _ in await service.collections(s)]
        counts = {c.id: await service.document_count(s, c) for c in cols}
    last = len(cols) - 1

    def arrow(c: Collection, direction: str, show: bool) -> Any:
        label = "↑" if direction == "up" else "↓"
        return (
            {"action": f"/api/docs/collections/{c.slug}/{direction}", "label": label}
            if show
            else ""
        )

    return {
        "rows": [
            {
                "name": {"text": c.name, "href": f"/docs/c/{c.slug}"},
                "description": c.description,
                "documents": counts[c.id],
                "up": arrow(c, "up", i > 0),
                "down": arrow(c, "down", i < last),
                "edit": {"text": "Edit", "href": f"/docs/collections/{c.slug}/edit"},
                "delete": {"text": "Delete", "href": f"/docs/collections/{c.slug}/delete"},
            }
            for i, c in enumerate(cols)
        ],
        "error": request.query_params.get("error"),
    }


@module.page(
    "/collections/new",
    template="collection_edit.html",
    title="New collection",
    permission="docs.collections.manage",
)
async def new_collection(request: Request) -> dict[str, Any]:
    kept = module.forms.recall(request)
    return {
        "col": None,
        "name": kept.get("name") if kept else "",
        "description": kept.get("description") if kept else "",
        "error": request.query_params.get("error"),
    }


@module.page(
    "/collections/{slug}/edit",
    template="collection_edit.html",
    title="Edit collection",
    permission="docs.collections.manage",
)
async def edit_collection(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        col = await _collection_or_404(s, request.path_params["slug"])
    kept = module.forms.recall(request)
    return {
        "page_title": f"Edit: {col.name}",
        "col": col,
        "name": kept.get("name") if kept else col.name,
        "description": kept.get("description") if kept else col.description,
        "error": request.query_params.get("error"),
    }


@module.page(
    "/collections/{slug}/delete",
    template="collection_delete.html",
    title="Delete collection",
    permission="docs.collections.manage",
)
async def delete_collection_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        col = await _collection_or_404(s, request.path_params["slug"])
        count = await service.document_count(s, col)
        others = [(c.id, c.name) for c, _ in await service.collections(s) if c.id != col.id]
    return {
        "page_title": f"Delete: {col.name}",
        "col": col,
        "count": count,
        "others": others,
        "error": request.query_params.get("error"),
    }


@module.router.post("/collections")
async def create_collection(
    request: Request,
    who: CanManageCollections,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.create_collection(s, name=name, description=description)
            await s.commit()
    except DocsError as exc:
        return await module.forms.back(request, "/docs/collections/new", str(exc))
    return _back("/docs/collections")


@module.router.post("/collections/{slug}")
async def update_collection(
    request: Request,
    slug: str,
    who: CanManageCollections,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        async with module.session() as s:
            col = await _collection_or_404(s, slug)
            await service.update_collection(s, col, name=name, description=description)
            await s.commit()
    except DocsError as exc:
        return await module.forms.back(request, f"/docs/collections/{slug}/edit", str(exc))
    return _back("/docs/collections")


async def _move(slug: str, direction: int) -> RedirectResponse:
    async with module.session() as s:
        col = await _collection_or_404(s, slug)
        await service.move_collection(s, col, direction)
        await s.commit()
    return _back("/docs/collections")


@module.router.post("/collections/{slug}/up")
async def collection_up(slug: str, who: CanManageCollections) -> RedirectResponse:
    return await _move(slug, -1)


@module.router.post("/collections/{slug}/down")
async def collection_down(slug: str, who: CanManageCollections) -> RedirectResponse:
    return await _move(slug, 1)


@module.router.post("/collections/{slug}/delete")
async def delete_collection(
    slug: str,
    who: CanManageCollections,
    move_to: Annotated[int | None, Form()] = None,
) -> RedirectResponse:
    try:
        async with module.session() as s:
            col = await _collection_or_404(s, slug)
            await service.delete_collection(s, col, move_to=move_to)
            await s.commit()
    except DocsError as exc:
        return _back(f"/docs/collections/{slug}/delete", str(exc))
    return _back("/docs/collections")
