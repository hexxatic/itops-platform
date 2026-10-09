"""MCP tools for Docs (exposed as ``docs_<name>`` on the platform's one MCP endpoint).

Tools run *via AI* as the connected user. They read, create drafts and save new draft
versions; they can never publish (``docs.publish`` is human-only). Every change goes
through ``service`` exactly like the web pages, so sanitizing, versioning and the draft
rule are identical for people and AI.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import ValidationError

from itops.sdk import Principal
from itops.sdk.content import Content, from_html, from_markdown, plain_text
from itops_docs import service
from itops_docs.app import module
from itops_docs.models import Document
from itops_docs.service import DocsError, known_fields, version_attachments, version_fields

CONTENT_HELP = (
    "Give the content in exactly ONE of three ways: "
    "`markdown` (simplest: '## Heading' lines become sections, the rest becomes text), "
    "`blocks` (a list of sections: {type:'heading', text, level:2|3}, "
    "{type:'text', html}, {type:'step', html, duration_min?}, {type:'html', html}) "
    "or `html` (rich HTML kept as written, e.g. collapsible sections and styled tables). "
    "Use step blocks for procedures: they are numbered automatically. "
    "Scripts, styles and external images are removed on save."
)
FIELDS_HELP = (
    "`fields` are the document type's values as {field_key: value} (see the type's fields "
    "with itops_get_type(module='docs', key=...)). Several values: a list. Links: "
    "'module:kind:id' (e.g. 'docs:document:<id>'). Credential fields take a 1Password "
    "reference (op://vault/item/field) or 1Password link, NEVER the secret itself. Required "
    "fields may be left empty in a draft; a person fills them before publishing."
)


def _who() -> Principal:
    p = module.current_principal()
    if p is None:
        raise PermissionError("not signed in")
    return p


def _content(
    markdown: str | None, blocks: list[dict[str, Any]] | None, html: str | None
) -> Content:
    given = [x is not None for x in (markdown, blocks, html)]
    if sum(given) != 1:
        raise DocsError("Provide exactly one of: markdown, blocks, html.")
    if markdown is not None:
        return from_markdown(markdown)
    if html is not None:
        return from_html(html)
    try:
        return Content.model_validate({"blocks": blocks})
    except ValidationError as exc:
        raise DocsError(f"Invalid blocks: {exc.errors()[0]['msg']}") from exc


def _summary(doc: Document) -> dict[str, Any]:
    return {
        "slug": doc.slug,
        "title": doc.title,
        "type": doc.type_key,
        "collection": doc.collection.slug,
        "status": doc.status,
        "updated": doc.updated_at.isoformat(timespec="minutes") + "Z",
        "url": module.url(f"/d/{doc.slug}"),
    }


async def _collection_id(s: Any, slug: str) -> int:
    col = await service.collection_by_slug(s, slug)
    if col is None:
        names = ", ".join(c.slug for c, _ in await service.collections(s))
        raise DocsError(f"Unknown collection {slug!r}. Choose one of: {names}.")
    return col.id


@module.tool(permission="docs.read")
async def list_collections() -> list[dict[str, Any]]:
    """List documentation collections (slug, name, description, document count).

    Use a collection's slug when creating documents or filtering lists."""
    async with module.session() as s:
        rows = await service.collections(s)
    return [
        {"slug": c.slug, "name": c.name, "description": c.description, "documents": n}
        for c, n in rows
    ]


@module.tool(permission="docs.read")
async def list_documents(
    collection: str | None = None,
    status: str | None = None,
    type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List documents, newest first. Optional filters: collection slug; status
    'draft' | 'published' | 'archived' (archived are hidden unless asked for); type (a
    document type key, see itops_list_types(module='docs'))."""
    if status not in (None, "draft", "published", "archived"):
        raise DocsError("status must be draft, published or archived.")
    async with module.session() as s:
        col_id = await _collection_id(s, collection) if collection else None
        docs = await service.documents(
            s, collection_id=col_id, status=status, limit=max(1, min(limit, 200))
        )
    return [_summary(d) for d in docs if type is None or d.type_key == type]


@module.tool(permission="docs.read")
async def search(
    query: str, limit: int = 10, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Search published documentation, best match first. Each result gives the
    document's title and slug, where in it the match is (e.g. 'Failover > Step 3'), a
    snippet and a `url` to cite. Read a hit in full with get_document(slug).
    Searches Docs only; itops_search covers every module. Archived documents are left
    out unless include_archived=true (results say which)."""
    hits = await module.search.query(
        query, scope="module", limit=max(1, min(limit, 30)), include_archived=include_archived
    )
    async with module.session() as s:
        slugs = await service.slugs_by_id(s, [h.entity_id for h in hits])
    return [
        {**h.as_dict(), "slug": slugs.get(h.entity_id), "url": module.absolute_url(h.url)}
        for h in hits
    ]


@module.tool(permission="docs.read")
async def get_document(slug: str, include_draft: bool = False) -> dict[str, Any]:
    """Read a document: its details, sections (blocks) and a plain-text version.

    Readers normally see the published version. Set include_draft=true to read the
    newest unpublished draft instead (e.g. before updating it). Cite documents to
    people by their `url`."""
    async with module.session() as s:
        doc = await service.by_slug(s, slug)
        if doc is None:
            raise DocsError(f"No document with slug {slug!r}. Use list_documents to find it.")
        v = await service.view(s, doc)
        tags = await service.tags(s, doc.id)
    content = v.content(prefer_draft=include_draft)
    shown = (v.draft or v.published) if include_draft else (v.published or v.draft)
    fields: dict[str, Any] = {}
    if doc.type_key:
        try:
            fields = known_fields(
                await service.type_definition(doc.type_key), version_fields(shown)
            )
        except DocsError:
            fields = version_fields(shown)
    return {
        **_summary(doc),
        "tags": tags,
        "version": shown.version_number if shown else None,
        "showing": "draft" if shown is not None and shown.published_at is None else "published",
        "has_unpublished_changes": v.has_pending_draft and v.published is not None,
        "fields": fields,
        "attachments": [
            {
                "filename": a["filename"],
                "size_bytes": a["size"],
                "url": module.absolute_url(f"/files/{a['id']}"),
            }
            for a in version_attachments(shown)
        ],
        "blocks": content.model_dump()["blocks"],
        "text": plain_text(content),
    }


async def create_document(
    title: str,
    collection: str,
    markdown: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    html: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new document. It is saved as a DRAFT: nobody sees it until a person
    reviews and publishes it in the web UI. Tell the user the returned `url` so they
    can review it. You cannot publish.

    `collection` is a collection slug (see list_collections). Optional `type` is a
    published document type key (itops_list_types(module='docs')), with its `fields`.
    """
    content = _content(markdown, blocks, html)
    async with module.session() as s:
        doc = await service.create(
            s,
            _who(),
            collection_id=await _collection_id(s, collection),
            title=title,
            content=content,
            tags=tags or [],
            type_key=type,
            fields=fields,
        )
        await s.commit()
        await s.refresh(doc, ["collection"])
        result = _summary(doc)
    return {
        **result,
        "next": f"Draft created. A person must review and publish it: {result['url']}",
    }


async def update_document(
    slug: str,
    change_note: str,
    markdown: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    html: str | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save new content for an existing document as a new DRAFT version. The published
    version stays what readers see until a person publishes the draft. Always send
    the complete document (not just the changed part): read it first with
    get_document(include_draft=true). `change_note` says what changed, for the history.
    For a typed document, `fields` replaces ALL field values (send them all); leave it out
    to keep the current values."""
    content = _content(markdown, blocks, html)
    if not change_note.strip():
        raise DocsError("Give a short change_note describing what changed.")
    async with module.session() as s:
        doc = await service.by_slug(s, slug)
        if doc is None:
            raise DocsError(f"No document with slug {slug!r}.")
        version = await service.save(s, _who(), doc, content, note=change_note, fields=fields)
        await s.commit()
        result = _summary(doc)
    return {
        **result,
        "version": version.version_number,
        "next": f"Draft saved. A person must review and publish it: {result['url']}?draft=1",
    }


# Registered here so their descriptions carry the content and fields guidance (AI clients
# read the tool description, not this module).
for _fn in (create_document, update_document):
    module.tool(
        permission="docs.write",
        writes=True,
        description="\n\n".join([inspect.getdoc(_fn) or "", CONTENT_HELP, FIELDS_HELP]),
    )(_fn)
