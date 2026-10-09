"""Document service: the one place documents change.

Browser pages and AI tools both call these functions, so the rules live here once:

* Creating or editing always makes a new **draft** version and never changes what
  readers see.
* **Publishing** (human-only: ``docs.publish``) stamps the latest version, repoints
  ``current_version_id`` and sets the next review date. Discarding and archiving are
  human-only too.
* Content is normalized (sanitized) on every save.
* **Attachments** (PDFs, Office files, Visio, ...) belong to a version like its fields:
  adding or removing one makes a draft. Stored as [{id, filename, size, mime}]; the bytes
  are in the core file store, claimed by the document.
* **Document types** (ADR 0008): a document may have a type (defined in the core type
  builder). Its field values are stored per version, so field edits are drafts too.
  Drafts may leave required fields empty; publishing needs them filled. Published values
  are indexed in docs.document_fields (queryable) and in search (not credential fields).

Callers pass a session and commit.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal
from itops.sdk.content import Content, Passage, normalize, passages
from itops.sdk.types import TypeDef, TypeProblem, TypeView
from itops_docs.app import module
from itops_docs.models import Collection, Document, DocumentField, DocumentTag, Version, utcnow

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FILE_RE = re.compile(r"/files/([0-9a-fA-F-]{36})")
MAX_TAGS = 20


class DocsError(ValueError):
    """A user-facing problem (shown as a message, not a crash)."""


def slugify(title: str) -> str:
    return _SLUG_RE.sub("-", title.lower()).strip("-")[:200] or "untitled"


def clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = " ".join(t.strip().lower().split())[:100]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


async def _unique_slug(s: AsyncSession, title: str, *, exclude: uuid.UUID | None = None) -> str:
    base = slugify(title)
    slug, n = base, 1
    while True:
        q = select(Document.id).where(Document.slug == slug)
        if exclude:
            q = q.where(Document.id != exclude)
        if (await s.execute(q)).first() is None:
            return slug
        n += 1
        slug = f"{base}-{n}"


def _file_ids(content: Content) -> list[str]:
    ids: list[str] = []
    for b in content.blocks:
        html = getattr(b, "html", "")
        ids += [m.lower() for m in _FILE_RE.findall(html)]
    return list(dict.fromkeys(ids))


async def latest_version(s: AsyncSession, doc_id: uuid.UUID) -> Version | None:
    """The newest version (draft or published)."""
    return await _latest(s, doc_id)


async def _latest(s: AsyncSession, doc_id: uuid.UUID) -> Version | None:
    return (
        await s.execute(
            select(Version)
            .where(Version.document_id == doc_id)
            .order_by(Version.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _set_tags(s: AsyncSession, doc_id: uuid.UUID, tags: list[str]) -> None:
    await s.execute(delete(DocumentTag).where(DocumentTag.document_id == doc_id))
    for t in clean_tags(tags):
        s.add(DocumentTag(document_id=doc_id, tag=t))


# ---- document types (ADR 0008) --------------------------------------------------------


async def available_types() -> list[TypeView]:
    """Types a new document can use: published and not retired."""
    return await module.types.list()


async def type_definition(type_key: str) -> TypeDef:
    """The published definition of a document type (retired types still have one)."""
    view = await module.types.get(type_key)
    if view is None or view.published is None:
        raise DocsError(f"There's no published document type {type_key!r}.")
    return view.published.definition


def version_attachments(version: Version | None) -> list[dict[str, Any]]:
    if version is None or not version.attachments:
        return []
    items: list[dict[str, Any]] = json.loads(version.attachments)
    return items


def version_fields(version: Version | None) -> dict[str, Any]:
    if version is None or not version.fields:
        return {}
    values: dict[str, Any] = json.loads(version.fields)
    return values


def known_fields(definition: TypeDef, values: dict[str, Any]) -> dict[str, Any]:
    """Values for fields the type still has (removed fields stay stored, just unused)."""
    keys = {f.key for f in definition.fields}
    return {k: v for k, v in values.items() if k in keys}


async def _validated(type_key: str, values: dict[str, Any], *, require: bool) -> dict[str, Any]:
    try:
        return await module.types.validate(type_key, values, require=require)
    except TypeProblem as exc:
        raise DocsError(str(exc)) from exc


async def _check_type_available(type_key: str) -> None:
    if type_key not in {t.key for t in await available_types()}:
        raise DocsError(f"{type_key!r} isn't an available document type (unknown or retired).")


async def _new_version(
    s: AsyncSession,
    doc: Document,
    content: Content,
    note: str,
    who: Principal,
    fields: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> Version:
    """``fields`` / ``attachments`` None keep the previous version's (content-only edits)."""
    content = normalize(content)
    # A typed document's fields can be its whole content (a Vendor record); a plain
    # document needs at least one section.
    if not content.blocks and not doc.type_key:
        raise DocsError("The document is empty. Add at least one section.")
    latest = await _latest(s, doc.id)
    if fields is None:
        fields = version_fields(latest)
    if attachments is None:
        attachments = version_attachments(latest)
    version = Version(
        document_id=doc.id,
        version_number=(latest.version_number + 1) if latest else 1,
        content=content.model_dump_json(),
        fields=json.dumps(fields) if fields else None,
        attachments=json.dumps(attachments) if attachments else None,
        change_note=note.strip()[:500],
        author_user=who.user,
        author_via_ai=who.via_ai,
        author_client=who.client,
    )
    s.add(version)
    doc.updated_at = utcnow()
    await s.flush()
    file_ids = _file_ids(content) + [str(a["id"]) for a in attachments]
    await module.files.claim(s, file_ids, entity_type="document", entity_id=str(doc.id))
    return version


# ---- reads --------------------------------------------------------------------------


async def collections(s: AsyncSession) -> list[tuple[Collection, int]]:
    """Collections with their count of non-archived documents."""
    counts = (
        select(Document.collection_id, func.count().label("n"))
        .where(Document.status != "archived")
        .group_by(Document.collection_id)
        .subquery()
    )
    rows = await s.execute(
        select(Collection, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.collection_id == Collection.id)
        .order_by(Collection.sort_order, Collection.name)
    )
    return [(c, int(n)) for c, n in rows.all()]


async def collection_by_slug(s: AsyncSession, slug: str) -> Collection | None:
    return (await s.execute(select(Collection).where(Collection.slug == slug))).scalar_one_or_none()


async def documents(
    s: AsyncSession,
    *,
    collection_id: int | None = None,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Document]:
    q = select(Document).order_by(Document.updated_at.desc()).limit(limit)
    if collection_id is not None:
        q = q.where(Document.collection_id == collection_id)
    if status:
        q = q.where(Document.status == status)
    elif not include_archived:
        q = q.where(Document.status != "archived")
    return list((await s.execute(q)).scalars().unique())


async def slugs_by_id(s: AsyncSession, ids: list[str]) -> dict[str, str]:
    """Document id (as indexed in search) -> current slug."""
    wanted = [uuid.UUID(i) for i in ids]
    if not wanted:
        return {}
    rows = await s.execute(select(Document.id, Document.slug).where(Document.id.in_(wanted)))
    return {str(i): slug for i, slug in rows.all()}


async def by_slug(s: AsyncSession, slug: str) -> Document | None:
    return (await s.execute(select(Document).where(Document.slug == slug))).scalar_one_or_none()


async def tags(s: AsyncSession, doc_id: uuid.UUID) -> list[str]:
    rows = await s.execute(
        select(DocumentTag.tag).where(DocumentTag.document_id == doc_id).order_by(DocumentTag.tag)
    )
    return list(rows.scalars())


async def versions(s: AsyncSession, doc_id: uuid.UUID) -> list[Version]:
    rows = await s.execute(
        select(Version).where(Version.document_id == doc_id).order_by(Version.version_number.desc())
    )
    return list(rows.scalars())


@dataclass
class DocView:
    """What a reader or editor needs: the document plus the right version to show."""

    doc: Document
    published: Version | None
    draft: Version | None  # newest version, if it's newer than the published one

    @property
    def has_pending_draft(self) -> bool:
        return self.draft is not None

    def content(self, *, prefer_draft: bool) -> Content:
        v = (self.draft or self.published) if prefer_draft else (self.published or self.draft)
        return Content.model_validate_json(v.content) if v else Content()


async def view(s: AsyncSession, doc: Document) -> DocView:
    latest = await _latest(s, doc.id)
    published = None
    if doc.current_version_id is not None:
        published = await s.get(Version, doc.current_version_id)
    draft = latest if latest and latest.published_at is None else None
    return DocView(doc=doc, published=published, draft=draft)


# ---- writes -------------------------------------------------------------------------


async def create(
    s: AsyncSession,
    who: Principal,
    *,
    collection_id: int,
    title: str,
    content: Content,
    tags: list[str] | None = None,
    type_key: str | None = None,
    fields: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> Document:
    """A new document as a draft (version 1). Readers don't see it until published.
    With ``type_key``, ``fields`` are checked against that type (required ones may still
    be empty: drafts can be incomplete)."""
    title = " ".join(title.split())[:300]
    if not title:
        raise DocsError("Give the document a title.")
    if await s.get(Collection, collection_id) is None:
        raise DocsError("Pick a collection.")
    values: dict[str, Any] = {}
    if type_key:
        await _check_type_available(type_key)
        values = await _validated(type_key, fields or {}, require=False)
    elif fields:
        raise DocsError("Fields need a document type.")
    doc = Document(
        collection_id=collection_id,
        title=title,
        slug=await _unique_slug(s, title),
        status="draft",
        type_key=type_key or None,
        created_by=who.user,
    )
    s.add(doc)
    await s.flush()
    await _new_version(s, doc, content, "Created", who, values, attachments or [])
    await _set_tags(s, doc.id, tags or [])
    await _audit(s, doc, "created", "Created as a draft")
    return doc


async def _audit(s: AsyncSession, doc: Document, verb: str, summary: str, **details: Any) -> None:
    await module.audit.record(
        s,
        f"document.{verb}",
        kind="document",
        entity_id=str(doc.id),
        title=doc.title,
        summary=summary,
        details=details or None,
    )


async def save(
    s: AsyncSession,
    who: Principal,
    doc: Document,
    content: Content,
    *,
    note: str = "",
    fields: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> Version:
    """Save edits as a new draft version. What readers see doesn't change. ``fields``
    None keeps the current field values; a dict replaces them (checked against the type)."""
    if doc.status == "archived":
        raise DocsError("This document is archived. Restore it before editing.")
    if fields is not None:
        if not doc.type_key:
            raise DocsError("This document has no type, so it has no fields.")
        fields = await _validated(doc.type_key, fields, require=False)
    before = {str(a["id"]) for a in version_attachments(await _latest(s, doc.id))}
    version = await _new_version(s, doc, content, note or "Edited", who, fields, attachments)
    after = {str(a["id"]) for a in version_attachments(version)}
    summary = f"Saved draft version {version.version_number}" + (f": {note}" if note else "")
    if added := len(after - before):
        summary += f"; added {added} attachment(s)"
    if removed := len(before - after):
        summary += f"; removed {removed} attachment(s)"
    await _audit(s, doc, "edited", summary, version=version.version_number)
    return version


async def update_details(
    s: AsyncSession,
    doc: Document,
    *,
    title: str,
    collection_id: int,
    tags: list[str],
    type_key: str | None = None,
    change_type: bool = False,
    who: Principal | None = None,
) -> None:
    """Title, collection, tags and (with ``change_type``) the type. Not versioned (they
    aren't content). Changing the type keeps every stored value; only the new type's
    fields are shown and checked. Details aren't drafted, so an AI connection (``who``
    via AI) may only change them on a document that was never published."""
    if who is not None and who.via_ai and doc.status != "draft":
        raise DocsError(
            "An AI connection can't change the title, collection, tags or type of a "
            "published document (they go live at once); a person does that in IT-Ops."
        )
    title = " ".join(title.split())[:300]
    if not title:
        raise DocsError("Give the document a title.")
    if await s.get(Collection, collection_id) is None:
        raise DocsError("Pick a collection.")
    changed: dict[str, Any] = {}
    if title != doc.title:
        changed["title"] = [doc.title, title]
        doc.title = title
        doc.slug = await _unique_slug(s, title, exclude=doc.id)
    if collection_id != doc.collection_id:
        old, new = (
            await s.get(Collection, doc.collection_id),
            await s.get(Collection, collection_id),
        )
        changed["collection"] = [old.name if old else None, new.name if new else None]
    doc.collection_id = collection_id
    old_tags = list(
        (
            await s.execute(select(DocumentTag.tag).where(DocumentTag.document_id == doc.id))
        ).scalars()
    )
    if sorted(clean_tags(tags)) != sorted(old_tags):
        changed["tags"] = [sorted(old_tags), sorted(clean_tags(tags))]
    if change_type and (type_key or None) != doc.type_key:
        if type_key:
            await _check_type_available(type_key)
        changed["type"] = [doc.type_key, type_key or None]
        doc.type_key = type_key or None
    doc.updated_at = utcnow()
    await _set_tags(s, doc.id, tags)
    await _index(s, doc)  # title, link and fields may have changed
    if changed:
        await _audit(s, doc, "details_changed", "Changed " + ", ".join(changed), **changed)


async def change_type(s: AsyncSession, doc: Document, type_key: str | None) -> None:
    """Give the document another type (or none). Not versioned: every stored value is
    kept; only the new type's fields are shown and checked."""
    if (type_key or None) == doc.type_key:
        return
    if type_key:
        await _check_type_available(type_key)
    before = doc.type_key
    doc.type_key = type_key or None
    doc.updated_at = utcnow()
    await _index(s, doc)
    await _audit(s, doc, "details_changed", "Changed type", type=[before, doc.type_key])


async def _index(s: AsyncSession, doc: Document) -> None:
    """Keep the core search index matching what readers see: the published version of
    published documents; archived ones stay in, flagged (hidden unless a search asks
    for them). Drafts are never searchable."""
    if doc.status == "draft" or doc.current_version_id is None:
        await module.search.remove(s, kind="document", entity_id=str(doc.id))
        return
    version = await s.get(Version, doc.current_version_id)
    if version is None:
        return
    extra = await _index_fields(s, doc, version)
    names = [str(a["filename"]) for a in version_attachments(version)]
    if names:
        extra.append(Passage(label="Attachments", anchor="attachments", text=", ".join(names)))
    await module.search.index(
        s,
        kind="document",
        entity_id=str(doc.id),
        title=doc.title,
        path=f"/d/{doc.slug}",
        passages=[*extra, *passages(Content.model_validate_json(version.content))],
        read_permission="docs.read",
        archived=doc.status == "archived",
    )


def field_text(value: Any) -> str:
    """A stored value as text (links are module:kind:id)."""
    if isinstance(value, dict):
        d: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return ":".join(str(d.get(k, "")) for k in ("module", "kind", "id"))
    return str(value)


async def _index_fields(s: AsyncSession, doc: Document, version: Version) -> list[Passage]:
    """Rebuild the queryable rows for the published field values; return a "Details"
    search passage (credential references are left out of search)."""
    await s.execute(delete(DocumentField).where(DocumentField.document_id == doc.id))
    if not doc.type_key:
        return []
    try:
        definition = await type_definition(doc.type_key)
    except DocsError:
        return []
    values = known_fields(definition, version_fields(version))
    lines: list[str] = []
    for f in definition.fields:
        raw = values.get(f.key)
        if raw is None or raw == "" or raw == []:
            continue
        items: list[Any] = raw if isinstance(raw, list) else [raw]  # pyright: ignore[reportUnknownVariableType]
        shown = [field_text(item) for item in items]
        for pos, item in enumerate(items):
            row = DocumentField(
                document_id=doc.id, field_key=f.key, position=pos, kind=f.kind,
                value_text=shown[pos][:450],
            )  # fmt: skip
            if f.kind == "date":
                row.value_date = dt.date.fromisoformat(str(item))
            elif f.kind == "number":
                row.value_number = float(item)
            s.add(row)
        if f.kind != "credential":
            lines.append(f"{f.label}: {', '.join(shown)}")
    if not lines:
        return []
    return [Passage(label="Details", anchor=None, text="\n".join(lines))]


@module.search.reindexer
async def reindex_all() -> int:
    """Rebuild every document's search entries (``itops search rebuild``)."""
    count = 0
    async with module.session() as s:
        for doc in (await s.execute(select(Document))).scalars().unique():
            await _index(s, doc)
            count += doc.status != "draft"
        await s.commit()
    return count


def _require_human(who: Principal) -> None:
    # The route/tool permission check is the real gate (docs.publish is human_only);
    # this is a second lock in case a future caller forgets it.
    if who.via_ai:
        raise DocsError("Publishing, discarding and archiving are done by a person.")


async def publish(s: AsyncSession, who: Principal, doc: Document) -> Version:
    """Publish the newest version: it becomes what readers see."""
    _require_human(who)
    version = await _latest(s, doc.id)
    if version is None:
        raise DocsError("Nothing to publish.")
    if doc.type_key:
        definition = await type_definition(doc.type_key)
        values = known_fields(definition, version_fields(version))
        try:
            await _validated(doc.type_key, values, require=True)
        except DocsError as exc:
            raise DocsError(f"Before publishing: {exc}") from exc
    now = utcnow()
    if version.published_at is None:
        version.published_at = now
    doc.status = "published"
    doc.current_version_id = version.id
    doc.updated_at = now
    doc.next_review_at = (now + dt.timedelta(days=doc.review_interval_days)).date()
    await s.flush()
    await _index(s, doc)
    await _audit(
        s, doc, "published", f"Published version {version.version_number}",
        version=version.version_number,
    )  # fmt: skip
    module.publish(
        "document_published",
        {
            "id": str(doc.id),
            "slug": doc.slug,
            "title": doc.title,
            "version": version.version_number,
        },
    )
    return version


async def discard(s: AsyncSession, who: Principal, doc: Document) -> str:
    """Throw away the unpublished draft.

    Returns ``"deleted"`` if the document was never published (it's removed entirely)
    or ``"reverted"`` (unpublished versions dropped, the published one stays).
    """
    _require_human(who)
    if doc.current_version_id is None:
        await _audit(s, doc, "deleted", "Discarded the draft (never published, so deleted)")
        await module.search.remove(s, kind="document", entity_id=str(doc.id))
        await s.execute(delete(Document).where(Document.id == doc.id))
        return "deleted"
    await s.execute(
        delete(Version).where(Version.document_id == doc.id, Version.published_at.is_(None))
    )
    doc.updated_at = utcnow()
    await _audit(s, doc, "draft_discarded", "Discarded the draft; the published version stays")
    return "reverted"


async def archive(s: AsyncSession, who: Principal, doc: Document) -> None:
    """Hide from browsing and search. Nothing is deleted; it can be restored."""
    _require_human(who)
    doc.status = "archived"
    doc.updated_at = utcnow()
    await _index(s, doc)  # flagged archived: hidden from search unless asked for
    await _audit(s, doc, "archived", "Archived")
    module.publish("document_archived", {"id": str(doc.id), "slug": doc.slug})


async def restore(s: AsyncSession, who: Principal, doc: Document) -> None:
    _require_human(who)
    doc.status = "published" if doc.current_version_id else "draft"
    doc.updated_at = utcnow()
    await _index(s, doc)
    await _audit(s, doc, "restored", "Restored from the archive")


# ---- collections (docs.collections.manage: admins, human-only) ----------------------------


async def _collection_audit(
    s: AsyncSession, col: Collection, verb: str, summary: str, **details: Any
) -> None:
    await module.audit.record(
        s, f"collection.{verb}", kind="collection", entity_id=str(col.id), title=col.name,
        summary=summary, details=details or None,
    )  # fmt: skip


def _collection_fields(name: str, description: str) -> tuple[str, str]:
    name = " ".join(name.split())[:200]
    if not name:
        raise DocsError("Give the collection a name.")
    return name, " ".join(description.split())[:1000]


async def create_collection(
    s: AsyncSession, *, name: str, description: str = "", slug: str | None = None
) -> Collection:
    """A new collection, last in the order. Its URL slug comes from the name (or ``slug``,
    for connectors that file into known collections) and stays fixed afterwards, so links
    to it keep working when it's renamed."""
    name, description = _collection_fields(name, description)
    if (
        await s.execute(select(Collection).where(func.lower(Collection.name) == name.lower()))
    ).first():
        raise DocsError(f"There's already a collection called “{name}”.")
    base = slugify(slug or name)[:90] or "collection"
    slug, n = base, 1
    while (await s.execute(select(Collection.id).where(Collection.slug == slug))).first():
        n += 1
        slug = f"{base}-{n}"
    last = (await s.execute(select(func.max(Collection.sort_order)))).scalar() or 0
    col = Collection(slug=slug, name=name, description=description, sort_order=last + 10)
    s.add(col)
    await s.flush()
    await _collection_audit(s, col, "created", f"Created collection “{name}”")
    return col


async def update_collection(
    s: AsyncSession, col: Collection, *, name: str, description: str
) -> None:
    name, description = _collection_fields(name, description)
    clash = await s.execute(
        select(Collection.id).where(
            func.lower(Collection.name) == name.lower(), Collection.id != col.id
        )
    )
    if clash.first():
        raise DocsError(f"There's already a collection called “{name}”.")
    changed: dict[str, Any] = {}
    if name != col.name:
        changed["name"] = [col.name, name]
    if description != col.description:
        changed["description"] = True
    col.name, col.description = name, description
    if changed:
        await _collection_audit(s, col, "updated", "Changed " + ", ".join(changed), **changed)


async def move_collection(s: AsyncSession, col: Collection, direction: int) -> None:
    """Swap places with the neighbour above (-1) or below (+1)."""
    ordered = [c for c, _ in await collections(s)]
    i = next(i for i, c in enumerate(ordered) if c.id == col.id)
    j = i + direction
    if not 0 <= j < len(ordered):
        return
    # Renumber the whole list so ties from older data can't make a move a no-op.
    ordered[i], ordered[j] = ordered[j], ordered[i]
    for pos, c in enumerate(ordered):
        c.sort_order = (pos + 1) * 10


async def document_count(s: AsyncSession, col: Collection) -> int:
    """Every document in it, archived ones included."""
    return int((await s.execute(
        select(func.count()).select_from(Document).where(Document.collection_id == col.id)
    )).scalar() or 0)  # fmt: skip


async def delete_collection(s: AsyncSession, col: Collection, *, move_to: int | None = None) -> int:
    """Delete a collection. If it still has documents (archived included), ``move_to``
    names the collection they move to first; nothing is ever deleted with it. Returns how
    many documents moved."""
    count = await document_count(s, col)
    target: Collection | None = None
    if count:
        target = await s.get(Collection, move_to) if move_to else None
        if target is None or target.id == col.id:
            raise DocsError(
                f"“{col.name}” still has {count} document(s). Choose a collection to move them to."
            )
        docs = (
            (await s.execute(select(Document).where(Document.collection_id == col.id)))
            .scalars()
            .unique()
        )
        for doc in docs:
            doc.collection_id = target.id
        await s.flush()
    summary = f"Deleted collection “{col.name}”"
    if target:
        summary += f"; moved {count} document(s) to “{target.name}”"
    await _collection_audit(
        s, col, "deleted", summary, moved=count, moved_to=target.name if target else None
    )
    await s.delete(col)
    return count


# ---- services for other modules (links between modules) ----------------------------------


def _parse_ids(ids: list[str]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for i in ids:
        try:
            out.append(uuid.UUID(str(i)))
        except ValueError:
            continue
    return out


def _link_ref(doc: Document) -> dict[str, str]:
    return {"id": str(doc.id), "kind": "document", "name": doc.title, "url": f"/docs/d/{doc.slug}"}


@module.service("lookup")
async def lookup(
    *,
    ids: list[str] | None = None,
    kind: str | None = None,
    query: str = "",
    limit: int = 50,
) -> list[dict[str, str]]:
    """Documents for link pickers and labels: by ``ids``, or by title ``query`` (archived
    left out). Each: {id, kind "document", name, url}. ``kind`` other than "document" finds
    nothing (documents are the only kind Docs links to)."""
    if kind not in (None, "document"):
        return []
    async with module.session() as s:
        q = select(Document)
        if ids is not None:
            wanted = _parse_ids(ids)
            if not wanted:
                return []
            q = q.where(Document.id.in_(wanted))
        else:
            q = q.where(Document.status != "archived").order_by(Document.title)
            q = q.limit(max(1, min(limit, 500)))
            if query.strip():
                q = q.where(Document.title.like(f"%{query.strip()}%"))
        docs = list((await s.execute(q)).scalars().unique())
    return [_link_ref(d) for d in docs]


@module.service("related")
async def related(link: str, limit: int = 50) -> list[dict[str, str]]:
    """Published documents whose fields link to ``link`` ("module:kind:id", e.g. an asset).
    Each: {id, kind, name, url, field} (field = the label of the linking field)."""
    async with module.session() as s:
        rows = (
            await s.execute(
                select(Document, DocumentField.field_key)
                .join(DocumentField, DocumentField.document_id == Document.id)
                .where(DocumentField.kind == "link", DocumentField.value_text == link)
                .where(Document.status == "published")
                .order_by(Document.title)
                .limit(max(1, min(limit, 200)))
            )
        ).all()
    labels: dict[tuple[str, str], str] = {}
    out: list[dict[str, str]] = []
    for doc, field_key in rows:
        type_key = doc.type_key or ""
        if (type_key, field_key) not in labels:
            try:
                fields = (await type_definition(type_key)).fields
                labels.update({(type_key, f.key): f.label for f in fields})
            except DocsError:
                pass
        out.append({**_link_ref(doc), "field": str(labels.get((type_key, field_key), field_key))})
    return out


@module.service("link_targets")
async def link_targets() -> list[dict[str, str]]:
    """What record-type link fields can point at in Docs (the type editor lists these)."""
    return [{"target": "docs", "label": "Docs: a document"}]
