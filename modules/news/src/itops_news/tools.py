"""MCP tools for News (exposed as ``news_<name>``).

Tools run *via AI* as the connected user. They read published news, and draft posts or
change drafts; they can never publish, and never change a published post
(``news.publish`` is human-only, and the service refuses AI on published posts).
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import ValidationError

from itops.sdk import Principal
from itops.sdk.content import Content, from_html, from_markdown, plain_text
from itops_news import service
from itops_news.app import module
from itops_news.models import Post
from itops_news.service import CATEGORIES, NewsError, post_content

CONTENT_HELP = (
    "Give the text as `markdown` (simplest: '## Heading' lines become sections) or `html`, "
    "not both. Keep it short: what changed or what's happening, and what people should do."
)
CATEGORY_HELP = "`category`: 'app' (an update to IT-Ops) or 'it' (an IT department announcement)."


def _who() -> Principal:
    p = module.current_principal()
    if p is None:
        raise PermissionError("not signed in")
    return p


def _content(markdown: str | None, html: str | None) -> Content:
    if (markdown is None) == (html is None):
        raise NewsError("Provide exactly one of: markdown, html.")
    try:
        return from_markdown(markdown) if markdown is not None else from_html(html or "")
    except ValidationError as exc:
        raise NewsError(f"Couldn't read the content: {exc.errors()[0]['msg']}") from exc


def _summary(p: Post) -> dict[str, Any]:
    return {
        "slug": p.slug,
        "title": p.title,
        "category": p.category,
        "kind": CATEGORIES[p.category],
        "status": p.status,
        "pinned": p.pinned,
        "published": p.published_at.isoformat(timespec="minutes") + "Z" if p.published_at else None,
        "url": module.url(f"/p/{p.slug}"),
    }


@module.tool(permission="news.read")
async def list_posts(category: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """What's new: published news posts, pinned first, then newest. Optional `category`:
    'app' (updates to IT-Ops) or 'it' (IT department announcements). Read one in full with
    get_post(slug); cite it to people by its `url`."""
    if category is not None and category not in CATEGORIES:
        raise NewsError("category must be 'app' or 'it'.")
    async with module.session() as s:
        posts = await service.feed(s, category=category, limit=max(1, min(limit, 50)))
    return [_summary(p) for p in posts]


@module.tool(permission="news.read")
async def get_post(slug: str) -> dict[str, Any]:
    """Read one news post in full (its text and details). Drafts only for people who can
    write news."""
    async with module.session() as s:
        post = await service.by_slug(s, slug)
    who = _who()
    if post is None or (post.status == "draft" and not who.has("news.write")):
        raise NewsError(f"No news post {slug!r}. Use list_posts to find it.")
    return {**_summary(post), "text": plain_text(post_content(post))}


async def draft_post(
    title: str, category: str, markdown: str | None = None, html: str | None = None
) -> dict[str, Any]:
    """Draft a news post. It stays a DRAFT that only news writers see until a person
    publishes it in the web UI; tell the user the returned `url` to review it. You cannot
    publish."""
    async with module.session() as s:
        post = await service.create(
            s, _who(), title=title, category=category, content=_content(markdown, html)
        )
        await s.commit()
        result = _summary(post)
    return {**result, "next": f"Draft saved. A person reviews and publishes it: {result['url']}"}


async def update_draft(
    slug: str,
    title: str | None = None,
    category: str | None = None,
    markdown: str | None = None,
    html: str | None = None,
) -> dict[str, Any]:
    """Change a DRAFT news post (published posts are changed by a person). Send the whole
    text again (read it with get_post first); leave out title/category to keep them."""
    async with module.session() as s:
        post = await service.by_slug(s, slug)
        if post is None:
            raise NewsError(f"No news post {slug!r}.")
        content = (
            post_content(post) if markdown is None and html is None else _content(markdown, html)
        )
        await service.save(
            s, _who(), post, title=title or post.title, category=category or post.category,
            content=content,
        )  # fmt: skip
        await s.commit()
        result = _summary(post)
    return {**result, "next": f"Draft saved. A person reviews and publishes it: {result['url']}"}


# Registered here so their descriptions carry the content guidance (AI clients read the
# tool description, not this module).
for _fn in (draft_post, update_draft):
    module.tool(
        permission="news.write",
        writes=True,
        description="\n\n".join([inspect.getdoc(_fn) or "", CONTENT_HELP, CATEGORY_HELP]),
    )(_fn)
