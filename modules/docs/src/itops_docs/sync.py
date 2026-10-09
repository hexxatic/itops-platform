"""Services for connectors (ADR 0010): how synced documentation gets into Docs.

A connector (e.g. the IT Glue import) calls these through the service registry while it
runs; they refuse to work outside a connector run and write nothing during a dry run.

- ``ensure_collections``: create or rename the collections it files documents into.
- ``source_status``: before fetching a document's images and files, ask whether it's new,
  unchanged, changed, or edited in IT-Ops since the last import (then it's left alone).
- ``save_file``: store an image or attachment as a Docs file (Docs owns and serves it).
- ``upsert``: create or update the document a source document maps to.

**People's edits win.** If the newest version of a synced document was written by a person,
or a person archived or restored it, a later run doesn't touch it; it reports "kept (edited
in IT-Ops)" instead. That's what lets someone clean a draft (say, move a password to
1Password) before publishing it. If a person only changed its details (title, collection,
tags, type), its content keeps following the source and those details stay as they set them.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import FileRejected, Principal
from itops.sdk.content import Content, normalize
from itops_docs import service
from itops_docs.app import module
from itops_docs.models import Document
from itops_docs.service import DocsError

KIND = "document"


def _run_and_who() -> tuple[Any, Principal]:
    run = module.refs.run
    if run is None:
        raise DocsError("Docs' sync services are for connector runs (ADR 0010).")
    who = module.current_principal()
    if who is None or not who.has("docs.write"):
        raise DocsError("The connector needs the docs.write permission.")
    return run, who


@module.service("ensure_collections")
async def ensure_collections(*, items: list[dict[str, str]]) -> dict[str, list[str]]:
    """Make sure these collections ({slug, name, description}) exist, by slug: missing ones
    are created, existing ones get the given name and description. Needs
    docs.collections.manage. Returns what was (or, in a dry run, would be) created/updated."""
    run, who = _run_and_who()
    if not who.has("docs.collections.manage"):
        raise DocsError("The connector needs the docs.collections.manage permission.")
    out: dict[str, list[str]] = {"created": [], "updated": [], "unchanged": []}
    async with module.session() as s:
        for item in items:
            slug, name = item["slug"], item["name"]
            description = item.get("description", "")
            col = await service.collection_by_slug(s, slug)
            if col is None:
                out["created"].append(name)
                if not run.dry_run:
                    await service.create_collection(
                        s, name=name, description=description, slug=slug
                    )
            elif col.name != name or col.description != description:
                out["updated"].append(name)
                if not run.dry_run:
                    await service.update_collection(s, col, name=name, description=description)
            else:
                out["unchanged"].append(name)
        if not run.dry_run:
            await s.commit()
    return out


# What people (anyone but the connector) did to a synced document, from its audit trail:
# their details stay; archiving or restoring it means it's theirs now.
DETAILS = "document.details_changed"
TAKEN_OVER = ("document.archived", "document.restored")


async def _people_did(doc: Document, who: Principal) -> set[str]:
    entries = await module.audit.history(kind=KIND, entity_id=str(doc.id), limit=500)
    return {e.action for e in entries
            if e.actor_user != who.user and e.action in (DETAILS, *TAKEN_OVER)}  # fmt: skip


async def _kept_by_people(s: AsyncSession, doc: Document, who: Principal) -> bool:
    """A person wrote its newest version, or archived or restored it: leave it alone."""
    latest = await service.latest_version(s, doc.id)
    if latest is not None and latest.author_user != who.user:
        return True
    return bool(await _people_did(doc, who) & set(TAKEN_OVER))


@module.service("source_status")
async def source_status(*, source_type: str, source_id: str, fingerprint: str) -> dict[str, str]:
    """``new`` | ``unchanged`` | ``changed`` | ``edited`` (a person changed it in IT-Ops
    since the import; it won't be overwritten). Ask before fetching images and files."""
    _run, who = _run_and_who()
    async with module.session() as s:
        ref = await module.refs.find(s, source_type, source_id)
        doc_id = _uuid(ref.entity_id) if ref else None
        doc = await s.get(Document, doc_id) if doc_id else None
        if ref is None or doc is None:
            return {"status": "new"}
        if await _kept_by_people(s, doc, who):
            return {"status": "edited", "id": str(doc.id)}
        if ref.fingerprint == fingerprint:
            return {"status": "unchanged", "id": str(doc.id)}
        return {"status": "changed", "id": str(doc.id)}


@module.service("save_file")
async def save_file(
    *, data: bytes, mime_type: str, filename: str, attachment: bool = False
) -> dict[str, Any]:
    """Store an image (``attachment=False``) or attachment as a Docs file, readable with
    docs.read. Returns {id, filename, size, mime} (an attachment entry for ``upsert``) or
    {problem} if the file is refused (type or size)."""
    run, _who = _run_and_who()
    if run.dry_run:
        return {"id": "dry-run", "filename": filename, "size": len(data), "mime": mime_type}
    try:
        stored = await module.files.save(
            data, mime_type=mime_type, filename=filename, attachment=attachment
        )
    except FileRejected as exc:
        return {"problem": f"{filename}: {exc}"}
    return {"id": stored.id, "filename": stored.filename, "size": stored.size_bytes,
            "mime": stored.mime_type}  # fmt: skip


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@module.service("upsert")
async def upsert(
    *,
    source_type: str,
    source_id: str,
    fingerprint: str,
    collection: str,
    title: str,
    content: Content,
    tags: list[str] | None = None,
    type_key: str | None = None,
    fields: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    published: bool = True,
    archived: bool = False,
    draft_reason: str = "",
) -> dict[str, Any]:
    """Create or update the document a source document maps to.

    ``collection`` is a collection slug (see ``ensure_collections``). ``published`` puts
    the new version live unless ``draft_reason`` says why a person should look first (it
    stays a draft and the reason is returned as a problem). A document a person has edited
    since the last import is left alone. Returns {outcome: created | updated | unchanged |
    kept | failed, id, problem}."""
    run, who = _run_and_who()
    try:
        content = normalize(content)
    except Exception as exc:
        return {"outcome": "failed", "problem": f"Content couldn't be converted: {exc}"}
    tags = tags or []
    async with module.session() as s:
        ref = await module.refs.find(s, source_type, source_id)
        doc_id = _uuid(ref.entity_id) if ref else None
        doc = await s.get(Document, doc_id) if doc_id else None
        col = await service.collection_by_slug(s, collection)
        if col is None and not run.dry_run:
            return {"outcome": "failed", "problem": f"No collection {collection!r}."}
        if doc is not None and ref is not None:
            if await _kept_by_people(s, doc, who):
                if not run.dry_run:
                    await module.refs.seen(s, ref, fingerprint=ref.fingerprint)
                    await s.commit()
                return {"outcome": "kept", "id": str(doc.id)}
            if ref.fingerprint == fingerprint:
                if not run.dry_run:
                    await module.refs.seen(s, ref, fingerprint=fingerprint)
                    await s.commit()
                return {"outcome": "unchanged", "id": str(doc.id)}
        try:
            if run.dry_run:
                return await _dry_check(title, type_key, fields, content, doc)
            assert col is not None
            if doc is None:
                doc = await service.create(
                    s, who, collection_id=col.id, title=title, content=content, tags=tags,
                    type_key=type_key, fields=fields, attachments=attachments or [],
                )  # fmt: skip
                outcome = "created"
                await module.refs.link(s, source_type, source_id, kind=KIND,
                                       entity_id=str(doc.id), fingerprint=fingerprint)  # fmt: skip
            else:
                if DETAILS not in await _people_did(doc, who):  # theirs stay as they set them
                    await service.update_details(
                        s, doc, title=title, collection_id=col.id, tags=tags,
                        type_key=type_key, change_type=True,
                    )  # fmt: skip
                if doc.status == "archived":  # saving needs it live; re-archived below
                    await service.restore(s, who, doc)
                await service.save(s, who, doc, content, note="Updated from the source",
                                   fields=fields, attachments=attachments or [])  # fmt: skip
                outcome = "updated"
                assert ref is not None
                await module.refs.seen(s, ref, fingerprint=fingerprint)
            problem = await _go_live(s, who, doc, published, archived, draft_reason)
            if archived and doc.status != "archived":  # e.g. it stayed a draft
                await service.archive(s, who, doc)
            await s.commit()
        except DocsError as exc:
            await s.rollback()
            return {"outcome": "failed", "problem": str(exc)}
    result: dict[str, Any] = {"outcome": outcome, "id": str(doc.id)}
    if problem:
        result["problem"] = problem
    return result


async def _go_live(
    s: AsyncSession, who: Principal, doc: Document, published: bool, archived: bool, reason: str
) -> str:
    """Publish (and archive) the new version when asked; returns why it stayed a draft."""
    if not published:
        return ""
    if reason:
        return f"Imported as a draft: {reason}"
    if not who.has("docs.publish"):
        return "Imported as a draft: the connector lacks docs.publish."
    try:
        await service.publish(s, who, doc)
    except DocsError as exc:
        return f"Imported as a draft: {exc}"
    if archived:
        await service.archive(s, who, doc)
    return ""


async def _dry_check(
    title: str,
    type_key: str | None,
    fields: dict[str, Any] | None,
    content: Content,
    doc: Document | None,
) -> dict[str, Any]:
    """What a real run would do, without writing."""
    if not " ".join(title.split()):
        raise DocsError("Give the document a title.")
    if not content.blocks and not type_key:
        raise DocsError("The document is empty.")
    if type_key:
        await service.type_definition(type_key)
        try:
            await module.types.validate(type_key, fields or {}, require=False)
        except ValueError as exc:
            raise DocsError(str(exc)) from exc
    return {"outcome": "updated" if doc else "created", "id": str(doc.id) if doc else None}
