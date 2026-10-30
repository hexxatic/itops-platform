"""MCP tools for Docs (exposed as ``docs_<name>`` on the platform's one MCP endpoint).

Tools run *via AI* as the connected user. They read, create drafts and save new draft
versions; they can never publish (``docs.publish`` is human-only). Every change goes
through ``service`` exactly like the web pages, so sanitizing, versioning and the draft
rule are identical for people and AI.
"""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import inspect
from pathlib import PurePath
from typing import Any

from pydantic import ValidationError

from itops.sdk import FileRejected, Principal
from itops.sdk.content import Content, from_html, from_markdown, plain_text
from itops_docs import service
from itops_docs.app import module
from itops_docs.models import Document
from itops_docs.service import (
    DocsError,
    known_fields,
    version_attachments,
    version_fields,
    version_logins,
)

CONTENT_HELP = (
    "Give the content in exactly ONE of three ways: "
    "`markdown` (simplest: '## Heading' lines become sections, the rest becomes text), "
    "`blocks` (a list of sections: {type:'heading', text, level:2|3}, "
    "{type:'text', html}, {type:'step', html, duration_min?}, {type:'html', html}) "
    "or `html` (rich HTML kept as written, e.g. collapsible sections and styled tables). "
    "Use step blocks for procedures: they are numbered automatically. "
    "Scripts, styles and external images are removed on save. For a screenshot, upload it "
    "first with upload_image and put the `markdown` it returns where the image goes."
)
FIELDS_HELP = (
    "`fields` are the document type's values as {field_key: value} (see the type's fields "
    "with itops_get_type(module='docs', key=...)). Several values: a list. Links: "
    "'module:kind:id' (e.g. 'docs:document:<id>'). Credential fields take a 1Password "
    "reference (op://vault/item/field) or 1Password link, NEVER the secret itself. Required "
    "fields may be left empty in a draft; a person fills them before publishing."
)
LOGINS_HELP = (
    "`logins` lists what the document logs into and where its secret lives in 1Password: "
    "[{label: 'Keyence console (admin)', ref: 'op://vault/item/field' or a 1Password item "
    "link}]. NEVER the password or secret itself, and never put a secret or a reference in "
    "the text: say 'log in as admin' and list the login here. Leave `logins` out to keep "
    "the current ones; a list replaces them all."
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
    'draft' | 'published' | 'archived' (archived are hidden unless asked for) | 'pending'
    (everything waiting for a person to publish it: new drafts and published documents
    with unpublished edits, each with the edit's version, change_note, who saved it and
    when); type (a document type key, see itops_list_types(module='docs')). Every
    document says whether it has_unpublished_changes."""
    if status not in (None, "draft", "published", "archived", "pending"):
        raise DocsError("status must be draft, published, archived or pending.")
    limit = max(1, min(limit, 200))
    async with module.session() as s:
        col_id = await _collection_id(s, collection) if collection else None
        waiting = await service.waiting(s, collection_id=col_id)
        if status == "pending":
            return [
                {
                    **_summary(d),
                    "has_unpublished_changes": d.status != "draft",
                    "pending_version": v.version_number,
                    "change_note": v.change_note,
                    "saved_by": v.author_user + (" (via AI)" if v.author_via_ai else ""),
                    "saved": v.created_at.isoformat(timespec="minutes") + "Z",
                }
                for d, v in waiting
                if type is None or d.type_key == type
            ][:limit]
        docs = await service.documents(s, collection_id=col_id, status=status, limit=limit)
    pending = {d.id for d, _ in waiting if d.status != "draft"}
    return [
        {**_summary(d), "has_unpublished_changes": d.id in pending}
        for d in docs
        if type is None or d.type_key == type
    ]


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
        "logins": version_logins(shown),
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
    logins: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Create a new document. It is saved as a DRAFT: nobody sees it until a person
    reviews and publishes it in the web UI. Pass on the returned `next` (it has the
    `review_url`). You cannot publish.

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
            logins=logins,
        )
        await s.commit()
        await s.refresh(doc, ["collection"])
        result = _summary(doc)
    return {**result, **module.review(f"/d/{doc.slug}", "the draft")}


async def update_document(
    slug: str,
    change_note: str,
    markdown: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    html: str | None = None,
    fields: dict[str, Any] | None = None,
    logins: list[dict[str, str]] | None = None,
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
        version = await service.save(
            s, _who(), doc, content, note=change_note, fields=fields, logins=logins
        )
        await s.commit()
        result = _summary(doc)
    return {
        **result,
        "version": version.version_number,
        **module.review(f"/d/{doc.slug}?draft=1", "the draft"),
    }


# Images are checked by their bytes, not by the name or type a client gives them.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_type(data: bytes) -> str | None:
    """PNG, JPEG, GIF or WebP by the file's first bytes; None for anything else."""
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@module.tool(permission="docs.write", writes=True)
async def upload_image(filename: str, content_base64: str, alt: str = "") -> dict[str, Any]:
    """Upload a screenshot or other image (PNG, JPEG, GIF or WebP, base64) to use in a
    document. Upload FIRST, then put the returned `markdown` where the image goes in the
    `markdown` content of create_document or update_document (or the `html` in html content
    or a text block): documents may only show images stored in IT-Ops. `alt` says what the
    image shows, for people who can't see it (defaults to the file name). Nothing is
    published: the image appears in the draft it's used in, and a person publishes that."""
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DocsError(
            "content_base64 isn't valid base64: send the image's bytes, encoded."
        ) from exc
    mime = image_type(data)
    if mime is None:
        raise DocsError("That isn't a PNG, JPEG, GIF or WebP image (judged by its contents).")
    name = PurePath(filename.replace("\\", "/")).name or "image"
    try:
        stored = await module.files.save(data, mime_type=mime, filename=name)
    except FileRejected as exc:
        raise DocsError(str(exc)) from exc
    alt = " ".join(alt.split()) or PurePath(name).stem
    md_alt = alt.replace("[", "(").replace("]", ")")
    return {
        "id": stored.id,
        "url": stored.url,
        "markdown": f"![{md_alt}]({stored.url})",
        "html": f'<img src="{stored.url}" alt="{html_lib.escape(alt)}">',
        "type": mime,
        "size": stored.size_bytes,
    }


# Registered here so their descriptions carry the content, fields and logins guidance (AI
# clients read the tool description, not this module).
for _fn in (create_document, update_document):
    module.tool(
        permission="docs.write",
        writes=True,
        description="\n\n".join(
            [inspect.getdoc(_fn) or "", CONTENT_HELP, FIELDS_HELP, LOGINS_HELP]
        ),
    )(_fn)
