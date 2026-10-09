"""MCP tools for assets (exposed as ``assets_<name>`` on the platform's one MCP endpoint).

Tools run *via AI* as the connected user. They read freely; they never change an asset
directly: ``propose_record`` and ``propose_change`` store proposals a person approves or
rejects on the Proposed changes page (``assets.publish`` is human-only). Every proposal is
checked exactly like a real save, so problems show up now, not at approval.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import Any

from itops.sdk import Principal
from itops_assets import service
from itops_assets.app import module
from itops_assets.models import Proposal, Record
from itops_assets.service import AssetsError, known_fields, proposal_fields, record_fields

FIELDS_HELP = (
    "`fields` are the asset type's values as {field_key: value}; see the type's fields with "
    "itops_get_type(module='assets', key=...). Dates: YYYY-MM-DD. Select fields take one of "
    "the type's options. Several values: a list. Links: 'module:kind:id', e.g. "
    "'assets:location:<id>' (find ids with list_records or search) or 'docs:document:<id>'. "
    "Credential fields take a 1Password reference (op://vault/item/field) or 1Password link, "
    "NEVER the secret itself. Never put passwords, keys or private keys in any field."
)


def _who() -> Principal:
    p = module.current_principal()
    if p is None:
        raise PermissionError("not signed in")
    return p


async def _type_names() -> dict[str, str]:
    return {k: v.current.definition.name for k, v in (await service.all_types()).items()}


def _summary(r: Record, type_names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "type": r.type_key,
        "type_name": type_names.get(r.type_key, r.type_key),
        "name": r.name,
        "url": module.url(f"/r/{r.id}"),
    }


async def _values(type_key: str, values: dict[str, Any]) -> dict[str, Any]:
    """Field values as an AI sees them: links as 'module:kind:id' plus the linked name."""
    with contextlib.suppress(AssetsError):  # a retired type: show what's stored
        values = known_fields(await service.type_definition(type_key), values)
    links = await module.links.resolve(values)
    out: dict[str, Any] = {}
    for key, raw in values.items():
        items: list[Any] = raw if isinstance(raw, list) else [raw]  # pyright: ignore[reportUnknownVariableType]
        shown: list[Any] = []
        for item in items:
            if isinstance(item, dict):
                ref = service.value_text(item)
                shown.append({"link": ref, "name": links.get(ref, {}).get("text", "(deleted)")})
            else:
                shown.append(item)
        out[key] = shown if isinstance(raw, list) else shown[0]
    return out


async def _record(record_id: str) -> Record:
    async with module.session() as s:
        record = await service.get(s, record_id)
    if record is None:
        raise AssetsError(f"No asset with id {record_id!r}. Find it with search or list_records.")
    return record


def _proposal(p: Proposal) -> dict[str, Any]:
    return {
        "proposal": p.id,
        "status": p.status,
        "change_to": str(p.record_id) if p.record_id else None,
        "type": p.type_key,
        "name": p.name,
        "fields": proposal_fields(p),
        "note": p.note,
        "proposed_by": p.proposed_by,
        "review_url": module.url("/proposals"),
    }


@module.tool(permission="assets.read")
async def list_records(type: str, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """List assets of one type by name (e.g. type='device'), optionally only those whose
    name or any field value contains `query` (plain text, e.g. an IP or serial number).
    Asset types: itops_list_types(module='assets'). Read one in full with get_record(id)."""
    names = await _type_names()
    if type not in names:
        raise AssetsError(f"Unknown asset type {type!r}. Types: {', '.join(sorted(names))}.")
    async with module.session() as s:
        found = await service.records(s, type_key=type, query=query, limit=max(1, min(limit, 200)))
    return [_summary(r, names) for r in found]


@module.tool(permission="assets.read")
async def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search every asset by meaning and keywords (e.g. 'the SQL server at head office',
    'certificate for vpn'), best match first. Each result has the asset's `id`, name, type
    and a snippet; read it in full with get_record(id). Searches assets only; itops_search
    covers every module."""
    hits = await module.search.query(query, scope="module", limit=max(1, min(limit, 30)))
    async with module.session() as s:
        records = {h.entity_id: await service.get(s, h.entity_id) for h in hits}
    names = await _type_names()
    out: list[dict[str, Any]] = []
    for h in hits:
        r = records.get(h.entity_id)
        if r is not None:
            out.append({**_summary(r, names), "snippet": h.as_dict()["snippet"]})
    return out


@module.tool(permission="assets.read")
async def get_record(id: str) -> dict[str, Any]:
    """Read one asset: its type, name and every field value (links show the linked asset's
    or document's name), the documents and other assets that link to it, and any changes
    waiting for approval. Cite it to people by its `url`."""
    record = await _record(id)
    link = f"assets:{record.type_key}:{record.id}"
    async with module.session() as s:
        waiting = [p for p in await service.pending_proposals(s) if p.record_id == record.id]
    return {
        **_summary(record, await _type_names()),
        "fields": await _values(record.type_key, record_fields(record)),
        "documents": [
            {"name": r["name"], "url": module.absolute_url(r["url"]), "linked_as": r["field"]}
            for r in await module.links.related(link)
            if r["module"] == "docs"
        ],
        "linked_from": [
            {"id": r["id"], "type": r["kind"], "name": r["name"], "linked_as": r["field"]}
            for r in await service.related(link)
        ],
        "pending_proposals": [_proposal(p) for p in waiting],
        "created": f"{record.created_at.isoformat(timespec='minutes')}Z by {record.created_by}",
        "updated": f"{record.updated_at.isoformat(timespec='minutes')}Z by {record.updated_by}",
    }


@module.tool(permission="assets.read")
async def list_proposals() -> list[dict[str, Any]]:
    """Asset changes waiting for a person to approve or reject (from any AI client), oldest
    first. Check here before proposing something that may already be waiting."""
    async with module.session() as s:
        return [_proposal(p) for p in await service.pending_proposals(s)]


async def propose_record(
    type: str, name: str, fields: dict[str, Any] | None = None, note: str = ""
) -> dict[str, Any]:
    """Propose a NEW asset. Nothing is created until a person approves it on the Proposed
    changes page; tell the user the returned `review_url`. You cannot approve.

    `type` is an asset type key (itops_list_types(module='assets')); `name` is the record's
    name (a device's hostname, a certificate's main host name, a domain, ...). `note` says
    where the information came from (e.g. 'from the Dell invoice'). Search first so you
    don't propose a duplicate."""
    async with module.session() as s:
        p = await service.propose(
            s, _who(), type_key=type, name=name, fields=fields or {}, note=note
        )
        await s.commit()
        result = _proposal(p)
    return {**result, "next": f"Proposed. A person must approve it: {result['review_url']}"}


async def propose_change(
    id: str, note: str, name: str | None = None, fields: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Propose a CHANGE to an existing asset. Nothing changes until a person approves it on
    the Proposed changes page; tell the user the returned `review_url`. You cannot approve.

    `fields` holds only what changes: each given key replaces that field, null or ''
    clears it, keys you leave out stay as they are. `name` renames it (leave out to keep).
    `note` says why and where the information came from. Read it first with get_record."""
    if not note.strip():
        raise AssetsError("Give a short note saying why, for the person who reviews it.")
    record = await _record(id)
    merged = {**record_fields(record)}
    for key, value in (fields or {}).items():
        if value is None or value == "" or value == []:
            merged.pop(key, None)
        else:
            merged[key] = value
    async with module.session() as s:
        current = await service.get(s, record.id)
        assert current is not None
        p = await service.propose(
            s, _who(), record=current, name=name or current.name, fields=merged, note=note
        )
        await s.commit()
        result = _proposal(p)
    return {**result, "next": f"Proposed. A person must approve it: {result['review_url']}"}


# Registered here so their descriptions carry the fields guidance (AI clients read the tool
# description, not this module).
for _fn in (propose_record, propose_change):
    module.tool(
        permission="assets.write",
        writes=True,
        description="\n\n".join([inspect.getdoc(_fn) or "", FIELDS_HELP]),
    )(_fn)
