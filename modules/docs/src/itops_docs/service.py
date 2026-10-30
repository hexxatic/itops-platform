"""Document service: the one place documents change.

Browser pages and AI tools both call these functions, so the rules live here once:

* Creating or editing always makes a new **draft** version and never changes what
  readers see.
* **Publishing** (human-only: ``docs.publish``) stamps the latest version, repoints
  ``current_version_id`` and sets the next review date. Discarding and archiving are
  human-only too. Publish and discard name the version the person reviewed and are
  refused if a newer draft was saved since (an AI connection may save one any time).
* Content is normalized (sanitized) on every save.
* **Attachments** (PDFs, Office files, Visio, ...) belong to a version like its fields:
  adding or removing one makes a draft. Stored as [{id, filename, size, mime}]; the bytes
  are in the core file store, claimed by the document.
* **Document types** (ADR 0008): a document may have a type (defined in the core type
  builder). Its field values are stored per version, so field edits are drafts too.
  Drafts may leave required fields empty; publishing needs them filled. Published values
  are indexed in docs.document_fields (queryable) and in search (not credential fields).

**Restricted collections** (ADR 0012): a collection is an area. Everything in one the
viewer can't see is left out of every read here (lists, counts, lookups, links, search)
and can't be written to: pages answer 404, tools say it doesn't exist.

**The help site** (ADR 0027, 0028): a publisher shows a document there, for everyone or one
**audience** (an area admins restrict to groups); see the how-tos section below.

Callers pass a session and commit.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal
from itops.sdk.content import Content, Passage, normalize, passages, plain_text
from itops.sdk.types import TypeDef, TypeProblem, TypeView, credential_ref
from itops_docs.app import module
from itops_docs.models import (
    Audience,
    Collection,
    Document,
    DocumentField,
    DocumentTag,
    Version,
    utcnow,
)

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


def version_logins(version: Version | None) -> list[dict[str, str]]:
    """The version's logins: [{label, ref}], each ref a 1Password reference or link."""
    if version is None or not version.logins:
        return []
    items: list[dict[str, str]] = json.loads(version.logins)
    return items


MAX_LOGINS = 30


def clean_logins(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Check logins before they're saved: every ref must be a 1Password reference or item
    link (never the secret itself); a row with neither label nor ref is dropped and a ref
    without a label is called "Login"."""
    out: list[dict[str, str]] = []
    for item in items:
        label = " ".join(str(item.get("label") or "").split())[:200]
        ref = str(item.get("ref") or "").strip()
        if not label and not ref:
            continue
        if not ref:
            raise DocsError(f"{label}: paste its 1Password reference or link.")
        try:
            out.append({"label": label or "Login", "ref": credential_ref(ref, label or "Login")})
        except TypeProblem as exc:
            raise DocsError(str(exc)) from exc
    if len(out) > MAX_LOGINS:
        raise DocsError(f"At most {MAX_LOGINS} logins per document.")
    return out


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
    logins: list[dict[str, Any]] | None = None,
) -> Version:
    """``fields`` / ``attachments`` / ``logins`` None keep the previous version's
    (content-only edits)."""
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
    logins = version_logins(latest) if logins is None else clean_logins(logins)
    version = Version(
        document_id=doc.id,
        version_number=(latest.version_number + 1) if latest else 1,
        content=content.model_dump_json(),
        fields=json.dumps(fields) if fields else None,
        attachments=json.dumps(attachments) if attachments else None,
        logins=json.dumps(logins) if logins else None,
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


async def hidden_collections() -> frozenset[int]:
    """Collections the current viewer can't see (ADR 0012); usually none."""
    return frozenset(int(k) for k in await module.areas.hidden("collection"))


async def collection_visible(s: AsyncSession, collection_id: int) -> Collection | None:
    """The collection, if it exists and the viewer can see it."""
    col = await s.get(Collection, collection_id)
    return col if col is not None and col.id not in await hidden_collections() else None


async def collections(s: AsyncSession) -> list[tuple[Collection, int]]:
    """Collections the viewer can see, with their count of non-archived documents."""
    counts = (
        select(Document.collection_id, func.count().label("n"))
        .where(Document.status != "archived")
        .group_by(Document.collection_id)
        .subquery()
    )
    q = (
        select(Collection, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.collection_id == Collection.id)
        .order_by(Collection.sort_order, Collection.name)
    )
    if hidden := await hidden_collections():
        q = q.where(Collection.id.not_in(hidden))
    rows = await s.execute(q)
    return [(c, int(n)) for c, n in rows.all()]


async def collection_by_slug(s: AsyncSession, slug: str) -> Collection | None:
    col = (await s.execute(select(Collection).where(Collection.slug == slug))).scalar_one_or_none()
    return col if col is not None and col.id not in await hidden_collections() else None


async def documents(
    s: AsyncSession,
    *,
    collection_id: int | None = None,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Document]:
    q = select(Document).order_by(Document.updated_at.desc()).limit(limit)
    if hidden := await hidden_collections():
        q = q.where(Document.collection_id.not_in(hidden))
    if collection_id is not None:
        q = q.where(Document.collection_id == collection_id)
    if status:
        q = q.where(Document.status == status)
    elif not include_archived:
        q = q.where(Document.status != "archived")
    return list((await s.execute(q)).scalars().unique())


async def waiting(
    s: AsyncSession, *, collection_id: int | None = None, limit: int = 200
) -> list[tuple[Document, Version]]:
    """Documents waiting to be published, last saved first: new drafts and published
    documents whose newest version is an unpublished edit. Each comes with that version
    (who saved it, when, its change note)."""
    q = await _waiting(select(Document, Version), collection_id)
    q = q.order_by(Version.created_at.desc()).limit(limit)
    return [(d, v) for d, v in (await s.execute(q)).unique().all()]


async def count_waiting(s: AsyncSession) -> int:
    """How many documents are waiting to be published (all of them, not a page)."""
    return await _count(s, await _waiting(select(Document.id)))


async def _waiting(q: Select[Any], collection_id: int | None = None) -> Select[Any]:
    newest = (
        select(Version.document_id, func.max(Version.version_number).label("n"))
        .group_by(Version.document_id)
        .subquery()
    )
    q = (
        q.join(newest, newest.c.document_id == Document.id)
        .join(
            Version,
            (Version.document_id == Document.id) & (Version.version_number == newest.c.n),
        )
        .where(Version.published_at.is_(None), Document.status != "archived")
    )
    if hidden := await hidden_collections():
        q = q.where(Document.collection_id.not_in(hidden))
    if collection_id is not None:
        q = q.where(Document.collection_id == collection_id)
    return q


async def _count(s: AsyncSession, q: Select[Any]) -> int:
    return int((await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one())


async def slugs_by_id(s: AsyncSession, ids: list[str]) -> dict[str, str]:
    """Document id (as indexed in search) -> current slug."""
    wanted = [uuid.UUID(i) for i in ids]
    if not wanted:
        return {}
    rows = await s.execute(select(Document.id, Document.slug).where(Document.id.in_(wanted)))
    return {str(i): slug for i, slug in rows.all()}


async def by_slug(s: AsyncSession, slug: str) -> Document | None:
    """The document, unless it's in a collection the viewer can't see."""
    doc = (await s.execute(select(Document).where(Document.slug == slug))).scalar_one_or_none()
    return doc if doc is not None and doc.collection_id not in await hidden_collections() else None


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


# ---- how-tos for staff (ADR 0027, 0028) ----------------------------------------------
# A publisher shows a document on the help site, for everyone or for one audience; the help
# site's How-tos (modules/kb) read them through the services below, as the person looking.
# An audience is an area (ADR 0012): restricted to AD groups, its how-tos are left out for
# anyone in none of them, the way a restricted collection's documents are. Audiences only
# narrow the help site: IT's Docs pages and search ignore them. Drafts, archived documents and
# documents in a restricted collection are never offered, whoever is asking.

EXCERPT_CHARS = 240


@dataclass
class EndUserDoc:
    doc: Document
    version: Version  # the published one
    tags: list[str]
    audience: str | None  # its name; None for everyone

    @property
    def content(self) -> Content:
        return Content.model_validate_json(self.version.content)


async def hidden_audiences() -> frozenset[int]:
    """Audiences the current viewer isn't in (ADR 0028); admins are in every one."""
    return frozenset(int(k) for k in await module.areas.hidden("audience"))


async def _offered_collections(s: AsyncSession, *, anyone: bool = False) -> list[Collection]:
    """Collections the viewer can see (``anyone``: every one) that aren't restricted to
    groups, in their order: a restricted collection is for its groups only, so nothing in it
    goes to everyone."""
    if anyone:
        q = select(Collection).order_by(Collection.sort_order, Collection.name)
        listed = list((await s.execute(q)).scalars())
    else:
        listed = [c for c, _ in await collections(s)]
    return [
        c for c in listed if not (await module.areas.access("collection", str(c.id)))["restricted"]
    ]


async def _with_versions(s: AsyncSession, docs: list[Document]) -> list[EndUserDoc]:
    if not docs:
        return []
    ids = [d.id for d in docs]
    versions = {
        v.id: v
        for v in (
            await s.execute(
                select(Version).where(Version.id.in_([d.current_version_id for d in docs]))
            )
        ).scalars()
    }
    tag_rows = await s.execute(
        select(DocumentTag.document_id, DocumentTag.tag)
        .where(DocumentTag.document_id.in_(ids))
        .order_by(DocumentTag.tag)
    )
    by_doc: dict[uuid.UUID, list[str]] = {}
    for doc_id, tag in tag_rows.all():
        by_doc.setdefault(doc_id, []).append(tag)
    names = {a.id: a.name for a in await audiences(s)}
    return [
        EndUserDoc(
            doc=d,
            version=v,
            tags=by_doc.get(d.id, []),
            audience=names.get(d.audience_id) if d.audience_id is not None else None,
        )
        for d in docs
        if d.current_version_id is not None and (v := versions.get(d.current_version_id))
    ]


async def help_documents(
    s: AsyncSession,
    *,
    collection: str | None = None,
    slug: str | None = None,
    every_audience: bool = False,
) -> list[EndUserDoc]:
    """Published documents shown on the help site that the viewer may see (``every_audience``:
    all of them, whoever is asking), by title; ``collection`` (a slug) or ``slug`` narrows
    them."""
    cols = {c.id: c for c in await _offered_collections(s, anyone=every_audience)}
    if collection is not None:
        cols = {i: c for i, c in cols.items() if c.slug == collection}
    if not cols:
        return []
    q = select(Document).where(
        Document.help_site == True,  # noqa: E712 (SQL Server has no IS TRUE)
        Document.status == "published",
        Document.current_version_id.is_not(None),
        Document.collection_id.in_(cols),
    )
    if not every_audience and (hidden := await hidden_audiences()):
        q = q.where(or_(Document.audience_id.is_(None), Document.audience_id.not_in(hidden)))
    if slug is not None:
        q = q.where(Document.slug == slug)
    docs = list((await s.execute(q.order_by(Document.title))).scalars().unique())
    return await _with_versions(s, docs)


async def help_collections(s: AsyncSession) -> list[Collection]:
    """The collections holding at least one how-to the viewer may see, in their order."""
    used = {e.doc.collection_id for e in await help_documents(s)}
    return [c for c in await _offered_collections(s) if c.id in used]


def excerpt(content: Content) -> str:
    """The first paragraph that isn't a heading, cut at a word near EXCERPT_CHARS."""
    for para in plain_text(content).split("\n\n"):
        if para.startswith("#"):
            continue
        if len(para) <= EXCERPT_CHARS:
            return para
        return para[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"
    return ""


def _howto(e: EndUserDoc) -> dict[str, Any]:
    return {
        "slug": e.doc.slug,
        "title": e.doc.title,
        "collection": {"slug": e.doc.collection.slug, "name": e.doc.collection.name},
        "audience": e.audience,
        # For the help site's search entries (ADR 0028): they're hidden like the audience.
        "area": module.areas.id("audience", str(e.doc.audience_id))
        if e.doc.audience_id is not None
        else None,
        "excerpt": excerpt(e.content),
        "tags": e.tags,
        "published_at": e.version.published_at,
        "version": e.version.version_number,
    }


@module.service("howto_collections")
async def howto_collections() -> list[dict[str, Any]]:
    """For the help site (ADR 0027): the collections holding how-tos the viewer may see, in
    their order: ``[{slug, name, description}]``."""
    async with module.session() as s:
        return [
            {"slug": c.slug, "name": c.name, "description": c.description}
            for c in await help_collections(s)
        ]


@module.service("howtos")
async def howtos(collection: str | None = None) -> list[dict[str, Any]]:
    """For the help site: every how-to the viewer may see (published, shown on the help site,
    for everyone or an audience they're in), by title: ``[{slug, title, collection: {slug,
    name}, audience, area, excerpt, tags, published_at, version}]``; ``audience`` is None
    for everyone."""
    async with module.session() as s:
        return [_howto(e) for e in await help_documents(s, collection=collection)]


@module.service("howto_index")
async def howto_index() -> list[dict[str, Any]]:
    """For the help site's search (kb), not for showing anyone: every how-to whatever its
    audience and whoever asks, as ``howtos`` gives them plus ``content``. Index each with its
    ``area`` (its audience's) so search hides it from people outside the audience."""
    async with module.session() as s:
        return [
            {**_howto(e), "content": e.content}
            for e in await help_documents(s, every_audience=True)
        ]


@module.service("howto")
async def howto(slug: str) -> dict[str, Any] | None:
    """For the help site: one how-to, the summary plus ``content`` (:class:`Content`) and
    ``attachments`` ``[{id, filename, size, mime}]``; None if it isn't one the viewer may
    see. Its images and attachments open to them too (``shared_file_permission``)."""
    async with module.session() as s:
        found = await help_documents(s, slug=slug)
        if not found:
            return None
        e = found[0]
        return {
            **_howto(e),
            "content": e.content,
            "attachments": [
                {
                    "id": a["id"],
                    "filename": a["filename"],
                    "size": a.get("size"),
                    "mime": a.get("mime"),
                }
                for a in version_attachments(e.version)
            ],
        }


@module.service("shared_file_permission")
async def shared_file_permission(entity_type: str, entity_id: str) -> str | None:
    """A document's files open to everyone (``docs.howto``) while it's on the help site:
    published, in a collection that isn't restricted (ADR 0027). Its audience is checked by
    ``visible_entity``, like a collection."""
    ids = _parse_ids([entity_id]) if entity_type == "document" else []
    if not ids:
        return None
    async with module.session() as s:
        doc = await s.get(Document, ids[0])
        if doc is None or doc.status != "published" or not doc.help_site:
            return None
        offered = {c.id for c in await _offered_collections(s)}
    return "docs.howto" if doc.collection_id in offered else None


async def set_help_site(
    s: AsyncSession, who: Principal, doc: Document, *, shown: bool, audience_id: int | None
) -> None:
    """Show the document on the help site (once it's published), for everyone or one
    audience, or take it off. A person does this: it puts the document in front of staff."""
    _require_human(who)
    if not shown:
        audience_id = None
    audience = await s.get(Audience, audience_id) if audience_id is not None else None
    if audience_id is not None and audience is None:
        raise DocsError("That audience doesn't exist any more. Choose another.")
    if (doc.help_site, doc.audience_id) == (shown, audience_id):
        return
    doc.help_site, doc.audience_id = shown, audience_id
    for_whom = f"for {audience.name}" if audience else "for everyone"
    summary = f"Shown on the help site {for_whom}" if shown else "Taken off the help site"
    await _audit(
        s, doc, "help_site", summary, shown=shown, audience=audience.name if audience else None
    )
    module.publish("help_site_changed", {"id": str(doc.id), "slug": doc.slug})


# ---- audiences (docs.collections.manage: admins, human-only) ---------------------------


async def audiences(s: AsyncSession) -> list[Audience]:
    """Every audience, in order (Docs' pages list them all, whoever is looking)."""
    return list(
        (await s.execute(select(Audience).order_by(Audience.sort_order, Audience.name))).scalars()
    )


async def audience_counts(s: AsyncSession) -> dict[int, int]:
    """Audience id -> how many documents are for it (archived included)."""
    rows = await s.execute(
        select(Document.audience_id, func.count())
        .where(Document.audience_id.is_not(None))
        .group_by(Document.audience_id)
    )
    return {int(a): int(n) for a, n in rows.all()}


async def _audience_audit(s: AsyncSession, a: Audience, verb: str, summary: str) -> None:
    await module.audit.record(
        s, f"audience.{verb}", kind="audience", entity_id=str(a.id), title=a.name, summary=summary
    )


def _audience_fields(name: str, description: str) -> tuple[str, str]:
    name = " ".join(name.split())[:200]
    if not name:
        raise DocsError("Give the audience a name, e.g. “Plant 2” or “Engineering”.")
    return name, " ".join(description.split())[:1000]


async def _audience_clash(s: AsyncSession, name: str, exclude: int | None = None) -> None:
    q = select(Audience.id).where(func.lower(Audience.name) == name.lower())
    if exclude is not None:
        q = q.where(Audience.id != exclude)
    if (await s.execute(q)).first():
        raise DocsError(f"There's already an audience called “{name}”.")


async def create_audience(s: AsyncSession, *, name: str, description: str = "") -> Audience:
    """A new audience, last in the order. It's open to everyone until an admin restricts it
    to groups on its access page."""
    name, description = _audience_fields(name, description)
    await _audience_clash(s, name)
    last = (await s.execute(select(func.max(Audience.sort_order)))).scalar() or 0
    a = Audience(name=name, description=description, sort_order=last + 10)
    s.add(a)
    await s.flush()
    await _audience_audit(s, a, "created", f"Created audience “{name}”")
    return a


async def update_audience(s: AsyncSession, a: Audience, *, name: str, description: str) -> None:
    name, description = _audience_fields(name, description)
    await _audience_clash(s, name, exclude=a.id)
    if (a.name, a.description) == (name, description):
        return
    summary = (
        f"Renamed audience “{a.name}” to “{name}”" if a.name != name else "Changed its description"
    )
    a.name, a.description = name, description
    await _audience_audit(s, a, "updated", summary)


async def delete_audience(s: AsyncSession, a: Audience) -> None:
    """Delete an audience nothing is for. One that documents are for is refused: moving them
    to everyone would show them to people it was chosen to keep them from."""
    n = (await audience_counts(s)).get(a.id, 0)
    if n:
        raise DocsError(
            f"{n} document(s) are for “{a.name}”. Choose another audience on each first "
            "(Documents, below)."
        )
    await _audience_audit(s, a, "deleted", f"Deleted audience “{a.name}”")
    await s.delete(a)


async def audience_documents(s: AsyncSession, a: Audience) -> list[Document]:
    """The documents for an audience, by title (archived included)."""
    q = select(Document).where(Document.audience_id == a.id).order_by(Document.title)
    return list((await s.execute(q)).scalars().unique())


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
    logins: list[dict[str, Any]] | None = None,
) -> Document:
    """A new document as a draft (version 1). Readers don't see it until published.
    With ``type_key``, ``fields`` are checked against that type (required ones may still
    be empty: drafts can be incomplete)."""
    title = " ".join(title.split())[:300]
    if not title:
        raise DocsError("Give the document a title.")
    if await collection_visible(s, collection_id) is None:
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
    await _new_version(s, doc, content, "Created", who, values, attachments or [], logins or [])
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
    logins: list[dict[str, Any]] | None = None,
) -> Version:
    """Save edits as a new draft version. What readers see doesn't change. ``fields``
    None keeps the current field values; a dict replaces them (checked against the type).
    ``logins`` likewise: None keeps them, a list replaces them."""
    if doc.status == "archived":
        raise DocsError("This document is archived. Restore it before editing.")
    if fields is not None:
        if not doc.type_key:
            raise DocsError("This document has no type, so it has no fields.")
        fields = await _validated(doc.type_key, fields, require=False)
    latest = await _latest(s, doc.id)
    before = {str(a["id"]) for a in version_attachments(latest)}
    logins_before = version_logins(latest)
    version = await _new_version(
        s, doc, content, note or "Edited", who, fields, attachments, logins
    )
    after = {str(a["id"]) for a in version_attachments(version)}
    summary = f"Saved draft version {version.version_number}" + (f": {note}" if note else "")
    if added := len(after - before):
        summary += f"; added {added} attachment(s)"
    if removed := len(before - after):
        summary += f"; removed {removed} attachment(s)"
    if version_logins(version) != logins_before:
        summary += "; changed logins"
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
    if await collection_visible(s, collection_id) is None:
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
    files = version_attachments(version)
    if files:
        names = ", ".join(str(a["filename"]) for a in files)
        extra.append(Passage(label="Attachments", anchor="attachments", text=names))
    extra += await attachment_passages(s, files, anchor="attachments")
    await module.search.index(
        s,
        kind="document",
        entity_id=str(doc.id),
        title=doc.title,
        path=f"/d/{doc.slug}",
        passages=[*extra, *passages(Content.model_validate_json(version.content))],
        read_permission="docs.read",
        archived=doc.status == "archived",
        area=("collection", str(doc.collection_id)),
    )


ATTACHMENT_TEXT_LIMIT = 400_000  # characters of attachment text indexed per document


async def attachment_passages(
    s: AsyncSession, files: list[dict[str, Any]], *, anchor: str | None
) -> list[Passage]:
    """What the attached files say, one passage per page ("Schedule.pdf, page 2"), so
    search finds a document by its attachments' contents (ADR 0015)."""
    out: list[Passage] = []
    budget = ATTACHMENT_TEXT_LIMIT
    for a in files:
        pages = await module.files.text(str(a["id"]), session=s)
        for n, text in enumerate(pages, start=1):
            if not text.strip() or budget <= 0:
                continue
            label = f"{a['filename']}, page {n}" if len(pages) > 1 else str(a["filename"])
            out.append(Passage(label=label, anchor=anchor, text=text[:budget]))
            budget -= len(text)
    return out


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
            elif f.kind in ("number", "money"):
                row.value_number = float(item)
            s.add(row)
        if f.kind != "credential":
            lines.append(f"{f.label}: {', '.join(shown)}")
    if not lines:
        return []
    return [Passage(label="Details", anchor=None, text="\n".join(lines))]


module.search.version = 2  # 2: attachment contents, by page (ADR 0015)


@module.search.reindexer
async def reindex_all() -> int:
    """Rebuild every document's search entries (``itops search rebuild``)."""
    count = 0
    async with module.session() as s:
        for doc in list((await s.execute(select(Document))).scalars().unique()):
            await _index(s, doc)
            await s.commit()  # one document at a time: never one long transaction
            count += doc.status != "draft"
        await s.commit()
    return count


def _require_human(who: Principal) -> None:
    # The route/tool permission check is the real gate (docs.publish is human_only);
    # this is a second lock in case a future caller forgets it.
    if who.via_ai:
        raise DocsError("Publishing, discarding and archiving are done by a person.")


def _check_reviewed(latest: Version, reviewed: int | None, done: str, verb: str) -> None:
    """Refuse when the newest version isn't the one the person had on screen (``reviewed``):
    an AI connection or a colleague may have saved another draft after the page loaded.
    ``None`` is only for a connector acting on the version it just saved itself."""
    if reviewed is None or latest.version_number == reviewed:
        return
    if latest.version_number > reviewed and latest.published_at is None:
        raise DocsError(
            f"Not {done}: v{latest.version_number} was saved by {latest.author_user}"
            f"{' (via AI)' if latest.author_via_ai else ''} after you opened v{reviewed}. "
            f"It's shown below: read it, then {verb} again."
        )
    raise DocsError(
        f"Not {done}: v{reviewed}, the version you opened, isn't the newest draft any more "
        "(it was published or discarded since). The page below shows where it stands now."
    )


async def publish(
    s: AsyncSession, who: Principal, doc: Document, *, version: int | None
) -> Version:
    """Publish the newest version: it becomes what readers see.

    ``version`` is the version number the person reviewed; publishing is refused if a newer
    one was saved since, so nothing goes live unread."""
    _require_human(who)
    latest = await _latest(s, doc.id)
    if latest is None:
        raise DocsError("Nothing to publish.")
    _check_reviewed(latest, version, "published", "publish")
    if doc.type_key:
        definition = await type_definition(doc.type_key)
        values = known_fields(definition, version_fields(latest))
        try:
            await _validated(doc.type_key, values, require=True)
        except DocsError as exc:
            raise DocsError(f"Before publishing: {exc}") from exc
    now = utcnow()
    if latest.published_at is None:
        latest.published_at = now
    doc.status = "published"
    doc.current_version_id = latest.id
    doc.updated_at = now
    doc.next_review_at = (now + dt.timedelta(days=doc.review_interval_days)).date()
    await s.flush()
    await _index(s, doc)
    await _audit(
        s, doc, "published", f"Published version {latest.version_number}",
        version=latest.version_number,
    )  # fmt: skip
    module.publish(
        "document_published",
        {
            "id": str(doc.id),
            "slug": doc.slug,
            "title": doc.title,
            "version": latest.version_number,
        },
    )
    return latest


async def discard(s: AsyncSession, who: Principal, doc: Document, *, version: int | None) -> str:
    """Throw away the unpublished draft.

    ``version`` is the draft the person reviewed; refused if a newer one was saved since.
    Returns ``"deleted"`` if the document was never published (it's removed entirely)
    or ``"reverted"`` (unpublished versions dropped, the published one stays).
    """
    _require_human(who)
    latest = await _latest(s, doc.id)
    if latest is not None:
        _check_reviewed(latest, version, "discarded", "discard it")
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
    s: AsyncSession,
    *,
    name: str,
    description: str = "",
    slug: str | None = None,
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
    s: AsyncSession,
    col: Collection,
    *,
    name: str,
    description: str,
) -> None:
    """Rename and describe the collection (its slug stays)."""
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
        module.publish("collection_changed", {"id": col.id, "slug": col.slug})


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
            await _index(s, doc)  # search knows which collection (area) it's in
        await s.flush()
    summary = f"Deleted collection “{col.name}”"
    if target:
        summary += f"; moved {count} document(s) to “{target.name}”"
    await _collection_audit(
        s, col, "deleted", summary, moved=count, moved_to=target.name if target else None
    )
    await s.delete(col)
    module.publish("collection_changed", {"id": col.id, "slug": col.slug})
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


def _restricted_ref(doc: Document) -> dict[str, str]:
    """A linked document the viewer can't see: no title, no address (ADR 0012)."""
    return {"id": str(doc.id), "kind": "document", "restricted": "1"}


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
    hidden = await hidden_collections()
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
            if hidden:
                q = q.where(Document.collection_id.not_in(hidden))
            if query.strip():
                q = q.where(Document.title.like(f"%{query.strip()}%"))
        docs = list((await s.execute(q)).scalars().unique())
    # By id (a link someone made): one the viewer can't see shows as "Restricted".
    return [_restricted_ref(d) if d.collection_id in hidden else _link_ref(d) for d in docs]


@module.service("related")
async def related(link: str, limit: int = 50) -> list[dict[str, str]]:
    """Published documents whose fields link to ``link`` ("module:kind:id", e.g. an asset).
    Each: {id, kind, name, url, field} (field = the label of the linking field)."""
    q = (
        select(Document, DocumentField.field_key)
        .join(DocumentField, DocumentField.document_id == Document.id)
        .where(DocumentField.kind == "link", DocumentField.value_text == link)
        .where(Document.status == "published")
        .order_by(Document.title)
        .limit(max(1, min(limit, 200)))
    )
    if hidden := await hidden_collections():
        q = q.where(Document.collection_id.not_in(hidden))
    async with module.session() as s:
        rows = (await s.execute(q)).all()
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


@module.service("describe_area")
async def describe_area(kind: str, key: str) -> dict[str, str] | None:
    """A collection's or audience's name and its page, for the core access page (ADR 0012)."""
    if kind not in ("collection", "audience") or not key.isdigit():
        return None
    async with module.session() as s:
        if kind == "audience":
            a = await s.get(Audience, int(key))
            return {"name": a.name, "url": f"/docs/audiences/{a.id}"} if a else None
        col = await s.get(Collection, int(key))
    return {"name": col.name, "url": f"/docs/collections/{col.slug}/edit"} if col else None


@module.service("area_entities")
async def area_entities(kind: str, key: str) -> dict[str, Any]:
    """The documents in a collection, as search entries (a new restriction marks them). An
    audience has none: it narrows only the help site, whose search kb filters (ADR 0028)."""
    if kind != "collection" or not key.isdigit():
        return {"kind": "document", "ids": []}
    async with module.session() as s:
        ids = await s.execute(select(Document.id).where(Document.collection_id == int(key)))
        return {"kind": "document", "ids": [str(i) for i in ids.scalars()]}


@module.service("visible_entity")
async def visible_entity(entity_type: str, entity_id: str) -> bool:
    """May the viewer see what a file belongs to (its document's collection; for someone who
    reads it on the help site, its audience too)? /files asks."""
    if entity_type != "document":
        return True
    wanted = _parse_ids([entity_id])
    if not wanted:
        return False
    async with module.session() as s:
        doc = await s.get(Document, wanted[0])
    if doc is None or doc.collection_id in await hidden_collections():
        return False
    who = module.current_principal()
    if who is None or who.has("docs.read") or doc.audience_id is None:
        return True
    return doc.audience_id not in await hidden_audiences()


@module.service("link_targets")
async def link_targets() -> list[dict[str, str]]:
    """What record-type link fields can point at in Docs (the type editor lists these)."""
    return [{"target": "docs", "label": "Docs: a document"}]


async def reviews_due(s: AsyncSession, *, limit: int = 200) -> list[Document]:
    """Published documents past their *Review by* date, longest overdue first."""
    q = (await _reviews_due(select(Document))).order_by(Document.next_review_at).limit(limit)
    return list((await s.execute(q)).scalars())


async def count_reviews_due(s: AsyncSession) -> int:
    """How many published documents are past their *Review by* date (all of them)."""
    return await _count(s, await _reviews_due(select(Document.id)))


async def _reviews_due(q: Select[Any]) -> Select[Any]:
    q = q.where(Document.status == "published", Document.next_review_at.is_not(None),
                Document.next_review_at <= dt.date.today())  # fmt: skip
    if hidden := await hidden_collections():
        q = q.where(Document.collection_id.not_in(hidden))
    return q


async def mark_reviewed(s: AsyncSession, who: Principal, doc: Document) -> None:
    """A person checked a published document and it's still right: its next review is one
    interval from today (audited). People only; AI can't sign a review off."""
    if who.via_ai:
        raise DocsError("A person signs a review off, in the web UI.")
    doc.next_review_at = dt.date.today() + dt.timedelta(days=doc.review_interval_days)
    await module.audit.record(
        s, "document.reviewed", kind="document", entity_id=str(doc.id), title=doc.title,
        summary=f"Reviewed {doc.title}: still right; next review {doc.next_review_at}",
    )  # fmt: skip


@module.service("waiting")
async def waiting_for_you() -> list[dict[str, Any]]:
    """What's waiting for the person looking (core's *Waiting for you*): drafts and edits to
    publish, and published documents due for review."""
    who = module.current_principal()
    out: list[dict[str, Any]] = []
    if who is None:
        return out
    async with module.session() as s:
        if who.has("docs.publish"):
            n = await count_waiting(s)
            out.append({"title": f"{n} document{'s' if n != 1 else ''} to publish",
                        "url": "/docs/#waiting", "count": n})  # fmt: skip
        if who.has("docs.write"):
            n = await count_reviews_due(s)
            out.append({"title": f"{n} document{'s' if n != 1 else ''} due for review",
                        "url": "/docs/#reviews", "count": n})  # fmt: skip
    return out
