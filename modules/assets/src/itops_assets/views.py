"""Assets pages (server-rendered, core components only) and the form actions behind them.

Pages live at /assets/...; actions are POSTs under /api/assets/... that redirect back to
a page (plain forms, no JavaScript needed). Problems come back as ``?error=...``.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from itops.sdk import Principal
from itops.sdk.types import TypeDef
from itops_assets import service
from itops_assets.app import module
from itops_assets.models import Proposal, Record
from itops_assets.service import AssetsError, known_fields, proposal_fields, record_fields

CanWrite = Annotated[Principal, Depends(module.require("assets.write"))]
CanPublish = Annotated[Principal, Depends(module.require("assets.publish"))]

LIST_COLUMNS = 4  # fields shown in a type's list, after the name
LIST_KINDS = ("text", "select", "date", "number", "url", "link")


def _when(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _noun(type_name: str) -> str:
    """A type name mid-sentence: 'Device' -> 'device', but 'SSL certificate' stays."""
    return type_name if type_name[1:2].isupper() else type_name[:1].lower() + type_name[1:]


def _back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


async def _record_or_404(s: Any, record_id: str) -> Record:
    record = await service.get(s, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return record


async def _type_or_404(type_key: str) -> tuple[str, TypeDef]:
    types = await service.all_types()
    view = types.get(type_key)
    if view is None or view.published is None:
        raise HTTPException(status_code=404, detail="Asset type not found")
    return view.published.definition.name, view.published.definition


async def _form_fields(request: Request) -> dict[str, Any]:
    """Field inputs (named f__<key>); repeated names = several values."""
    form = await request.form()
    out: dict[str, Any] = {}
    for key in {k for k in form if k.startswith("f__")}:
        values = [str(v) for v in form.getlist(key) if str(v).strip()]
        out[key[3:]] = values if len(values) > 1 else (values[0] if values else "")
    return out


def _cell(field_kind: str, value: Any, links: dict[str, Any]) -> Any:
    if value is None or value == "" or value == []:
        return ""
    items: list[Any] = value if isinstance(value, list) else [value]  # pyright: ignore[reportUnknownVariableType]
    if field_kind == "link":
        keys = [service.value_text(i) for i in items]
        return ", ".join(links.get(k, {}).get("text", "(deleted)") for k in keys)
    return ", ".join(service.value_text(i) for i in items)


# ---- pages --------------------------------------------------------------------------------


@module.page("/", template="index.html", title="Assets", nav="Assets", permission="assets.read")
async def home(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        counts = await service.counts_by_type(s)
        pending = len(await service.pending_proposals(s)) if _can("assets.publish") else 0
    return {
        "rows": [
            {
                "type": {"text": t.current.definition.name, "href": f"/assets/t/{t.key}"},
                "count": counts.get(t.key, 0),
                "about": t.current.definition.description,
            }
            for t in await service.available_types()
        ],
        "pending": pending,
        "can_manage_types": _can("assets.types.draft"),
        "error": request.query_params.get("error"),
    }


@module.page("/t/{type_key}", template="type_list.html", title="Assets", permission="assets.read")
async def type_list(request: Request) -> dict[str, Any]:
    type_key = request.path_params["type_key"]
    type_name, definition = await _type_or_404(type_key)
    query = request.query_params.get("q", "")
    async with module.session() as s:
        found = await service.records(s, type_key=type_key, query=query, limit=500)
    # A field called "name" (imported types have them) would just repeat the record's name.
    fields = [f for f in definition.fields if f.kind in LIST_KINDS and f.key != "name"]
    columns = fields[:LIST_COLUMNS]
    links = await module.links.resolve(*(record_fields(r) for r in found))
    # Field cells are keyed "f:<key>", so no field can overwrite the record-name link.
    rows = [
        {
            "name": {"text": r.name, "href": f"/assets/r/{r.id}"},
            **{f"f:{f.key}": _cell(f.kind, record_fields(r).get(f.key), links) for f in columns},
        }
        for r in found
    ]
    return {
        "page_title": type_name,
        "type_key": type_key,
        "type_name": type_name,
        "type_noun": _noun(type_name),
        "description": definition.description,
        "columns": [("name", "Name")] + [(f"f:{f.key}", f.label) for f in columns],
        "rows": rows,
        "query": query,
        "can_write": _can("assets.write"),
    }


@module.page("/r/{record_id}", template="record.html", title="Asset", permission="assets.read")
async def record_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        record = await _record_or_404(s, request.path_params["record_id"])
    types = await service.all_types()
    view = types.get(record.type_key)
    definition = view.published.definition if view and view.published else None
    values = record_fields(record)
    if definition:
        values = known_fields(definition, values)
    link = f"assets:{record.type_key}:{record.id}"
    documents = [r for r in await module.links.related(link) if r["module"] == "docs"]
    sources = await module.refs.links(record.type_key, str(record.id))
    by = {f"connector:{src.connector}": f"{src.title} (connector)" for src in sources}
    type_names = {k: v.current.definition.name for k, v in types.items()}
    return {
        "page_title": record.name,
        "record": record,
        "type_name": definition.name if definition else record.type_key,
        "type_noun": _noun(definition.name if definition else record.type_key),
        "definition": definition,
        "values": values,
        "links": await module.links.resolve(values),
        "sources": [
            {
                "system": src.system,
                "title": src.title,
                "last_seen": _when(src.last_seen),
                "missing_since": _when(src.missing_since),
            }
            for src in sources
        ],
        "documents": [
            {"name": {"text": r["name"], "href": r["url"]}, "field": r["field"]} for r in documents
        ],
        "linked_from": [
            {
                "name": {"text": r["name"], "href": r["url"]},
                "type": type_names.get(r["kind"], r["kind"]),
                "field": r["field"],
            }
            for r in await service.related(link)
        ],
        "created": _when(record.created_at),
        "created_by": by.get(record.created_by, record.created_by),
        "updated_by": by.get(record.updated_by, record.updated_by),
        "updated": _when(record.updated_at),
        "activity": await module.audit.history(kind=record.type_key, entity_id=str(record.id)),
        "can_write": _can("assets.write"),
        "can_delete": _can("assets.publish"),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/t/{type_key}/new", template="record_edit.html", title="New asset", permission="assets.write"
)
async def new_record(request: Request) -> dict[str, Any]:
    type_key = request.path_params["type_key"]
    type_name, definition = await _type_or_404(type_key)
    kept = module.forms.recall(request)
    return {
        "page_title": f"New {_noun(type_name)}",
        "type_noun": _noun(type_name),
        "locked": [],
        "name_locked": False,
        "locked_note": "",
        "record": None,
        "name": kept.get("name") if kept else "",
        "type_key": type_key,
        "type_name": type_name,
        "definition": definition,
        "values": kept.fields() if kept else {},
        "link_options": await module.links.options(definition),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/r/{record_id}/edit",
    template="record_edit.html",
    title="Edit asset",
    permission="assets.write",
)
async def edit_record(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        record = await _record_or_404(s, request.path_params["record_id"])
    type_name, definition = await _type_or_404(record.type_key)
    kept = module.forms.recall(request)
    owned = await service.owned(record)
    sources = sorted(
        {src.system for src in await module.refs.links(record.type_key, str(record.id))}
    )
    return {
        "page_title": f"Edit {record.name}",
        "record": record,
        "locked": sorted(owned),
        "name_locked": "name" in owned or "*" in owned,
        "locked_note": f"Synced from {', '.join(sources)}: change it there." if sources else "",
        "name": kept.get("name") if kept else record.name,
        "type_key": record.type_key,
        "type_name": type_name,
        "definition": definition,
        "values": kept.fields() if kept else known_fields(definition, record_fields(record)),
        "link_options": await module.links.options(definition),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/proposals", template="proposals.html", title="Proposed changes", permission="assets.publish"
)
async def proposals(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        pending = await service.pending_proposals(s)
        current = {p.id: await service.get(s, p.record_id) for p in pending if p.record_id}
    types = await service.all_types()
    items: list[dict[str, Any]] = []
    for p in pending:
        view = types.get(p.type_key)
        definition = view.published.definition if view and view.published else None
        items.append(await _proposal_view(p, current.get(p.id), definition))
    return {"items": items, "error": request.query_params.get("error")}


def _differs(record: Record | None, before: Any, after: Any) -> bool:
    # A change shows what differs; a new asset shows what it has.
    return before != after if record else after not in (None, "", [])


async def _proposal_view(
    p: Proposal, record: Record | None, definition: TypeDef | None
) -> dict[str, Any]:
    """What a person needs to decide: the values of a new asset, or before -> after."""
    after = proposal_fields(p)
    before = record_fields(record) if record else {}
    links = await module.links.resolve(before, after)
    fields = [(f.key, f.label, f.kind) for f in definition.fields] if definition else []
    known = {k for k, _, _ in fields}
    fields += [(k, k, "text") for k in sorted((set(before) | set(after)) - known)]
    rows = [
        {"field": label, "before": _cell(kind, before.get(key), links),
         "after": _cell(kind, after.get(key), links)}
        for key, label, kind in fields
        if _differs(record, before.get(key), after.get(key))
    ]  # fmt: skip
    if record and record.name != p.name:
        rows.insert(0, {"field": "Name", "before": record.name, "after": p.name})
    type_name = definition.name if definition else p.type_key
    return {
        "id": p.id,
        "title": f"Change to {record.name}" if record else f"New {_noun(type_name)}: {p.name}",
        "href": f"/assets/r/{record.id}" if record else None,
        "by": p.proposed_by + (" (via AI)" if p.proposed_via_ai else ""),
        "when": _when(p.created_at),
        "note": p.note,
        "rows": rows,
        "is_change": record is not None,
    }


# ---- actions -------------------------------------------------------------------------------


@module.router.post("/records")
async def create_record(
    request: Request,
    who: CanWrite,
    type_key: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    fields = await _form_fields(request)
    try:
        async with module.session() as s:
            record = await service.create(s, who, type_key=type_key, name=name, fields=fields)
            await s.commit()
            rid = record.id
    except AssetsError as exc:
        return await module.forms.back(request, f"/assets/t/{type_key}/new", str(exc))
    return _back(f"/assets/r/{rid}")


@module.router.post("/records/{record_id}")
async def update_record(
    request: Request, record_id: str, who: CanWrite, name: Annotated[str, Form()] = ""
) -> RedirectResponse:
    fields = await _form_fields(request)
    try:
        async with module.session() as s:
            record = await _record_or_404(s, record_id)
            await service.update(s, who, record, name=name, fields=fields)
            await s.commit()
    except AssetsError as exc:
        return await module.forms.back(request, f"/assets/r/{record_id}/edit", str(exc))
    return _back(f"/assets/r/{record_id}")


@module.router.post("/records/{record_id}/delete")
async def delete_record(record_id: str, who: CanPublish) -> RedirectResponse:
    try:
        async with module.session() as s:
            record = await _record_or_404(s, record_id)
            type_key = record.type_key
            await service.delete_record(s, who, record)
            await s.commit()
    except AssetsError as exc:
        return _back(f"/assets/r/{record_id}", str(exc))
    return _back(f"/assets/t/{type_key}")


async def _decide(proposal_id: int, who: Principal, approve: bool) -> RedirectResponse:
    try:
        async with module.session() as s:
            p = await s.get(Proposal, proposal_id)
            if p is None:
                raise HTTPException(status_code=404, detail="Proposal not found")
            await service.decide(s, who, p, approve=approve)
            await s.commit()
    except AssetsError as exc:
        return _back("/assets/proposals", str(exc))
    return _back("/assets/proposals")


@module.router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: int, who: CanPublish) -> RedirectResponse:
    return await _decide(proposal_id, who, True)


@module.router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: int, who: CanPublish) -> RedirectResponse:
    return await _decide(proposal_id, who, False)
