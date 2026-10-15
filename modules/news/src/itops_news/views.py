"""News pages (server-rendered, core components only) and the form actions behind them.

Pages live at /news/...; actions are POSTs under /api/news/... that redirect back to a
page. Problems come back as ``?error=...``; a failed save keeps the form.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from itops.sdk import KeptForm, Principal
from itops.sdk.content import Content, render
from itops_news import service
from itops_news.app import module
from itops_news.models import Post
from itops_news.service import CATEGORIES, NewsError, post_content

module.files.upload_route(permission="news.write")

CanWrite = Annotated[Principal, Depends(module.require("news.write"))]
CanPublish = Annotated[Principal, Depends(module.require("news.publish"))]

PAGE_SIZE = 20
CATEGORY_TONE = {"app": "info", "it": "success"}


def _when(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


def _back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


def _item(p: Post) -> dict[str, Any]:
    """What the feed and a post page show about a post."""
    return {
        "slug": p.slug,
        "title": p.title,
        "href": f"/news/p/{p.slug}",
        "category": CATEGORIES[p.category],
        "tone": CATEGORY_TONE[p.category],
        "pinned": p.pinned,
        "status": p.status,
        "when": _when(p.published_at or p.updated_at),
        "by": p.published_by or p.created_by,
        "blocks": render(post_content(p)),
    }


async def _post_or_404(s: Any, slug: str) -> Post:
    post = await service.by_slug(s, slug)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.status == "draft" and not _can("news.write"):
        raise HTTPException(status_code=404, detail="Post not found")  # drafts are for writers
    return post


def _kept_content(kept: KeptForm | None, fallback: Content) -> Content:
    if kept is None or "content" not in kept:
        return fallback
    try:
        return Content.model_validate_json(kept.get("content") or "{}")
    except ValidationError:
        return fallback


def _parse_content(raw: str) -> Content:
    try:
        return Content.model_validate_json(raw or "{}")
    except ValidationError as exc:
        raise NewsError("The editor sent content the server couldn't read.") from exc


# ---- pages ------------------------------------------------------------------------------


@module.page("/", template="index.html", title="News", nav="News", permission="news.read")
async def home(request: Request) -> dict[str, Any]:
    category = request.query_params.get("category") or None
    if category not in CATEGORIES:
        category = None
    page = max(1, int(request.query_params.get("page", "1") or 1))
    async with module.session() as s:
        posts = await service.feed(
            s, category=category, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE
        )
        drafts = await service.drafts(s) if _can("news.write") else []
    filters = [("", "Everything"), *CATEGORIES.items()]
    return {
        "posts": [_item(p) for p in posts[:PAGE_SIZE]],
        "drafts": [
            {
                "title": {"text": d.title, "href": f"/news/p/{d.slug}"},
                "category": CATEGORIES[d.category],
                "by": d.created_by + (" (via AI)" if d.created_via_ai else ""),
                "updated": _when(d.updated_at),
            }
            for d in drafts
        ],
        "filters": [
            {
                "label": label,
                "href": "/news/" + (f"?category={key}" if key else ""),
                "current": (key or None) == category,
            }
            for key, label in filters
        ],
        "category": category,
        "page": page,
        "older": f"/news/?page={page + 1}" + (f"&category={category}" if category else "")
        if len(posts) > PAGE_SIZE
        else None,
        "newer": f"/news/?page={page - 1}" + (f"&category={category}" if category else "")
        if page > 1
        else None,
        "can_write": _can("news.write"),
        "can_publish": _can("news.publish"),
        "error": request.query_params.get("error"),
    }


@module.page("/archive", template="archive.html", title="Archived news", permission="news.read")
async def archive_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        posts = await service.archived(s)
    return {
        "rows": [
            {
                "title": {"text": p.title, "href": f"/news/p/{p.slug}"},
                "category": CATEGORIES[p.category],
                "published": _when(p.published_at),
            }
            for p in posts
        ]
    }


@module.page("/p/{slug}", template="post.html", title="News", permission="news.read")
async def post_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        post = await _post_or_404(s, request.path_params["slug"])
    activity = (
        await module.audit.history(kind="post", entity_id=str(post.id))
        if _can("news.write")
        else []
    )
    can_publish = _can("news.publish")
    return {
        "page_title": post.title,
        "post": _item(post),
        "activity": activity,
        "can_edit": (post.status == "draft" and _can("news.write")) or can_publish,
        "can_publish": can_publish,
        "error": request.query_params.get("error"),
    }


@module.page("/new", template="edit.html", title="New post", permission="news.write")
async def new(request: Request) -> dict[str, Any]:
    kept = module.forms.recall(request)
    return {
        "post": None,
        "title": kept.get("title") if kept else "",
        "category": kept.get("category")
        if kept
        else (request.query_params.get("category") or "it"),
        "categories": list(CATEGORIES.items()),
        "content": _kept_content(kept, Content()).model_dump(),
        "error": request.query_params.get("error"),
    }


@module.page("/p/{slug}/edit", template="edit.html", title="Edit post", permission="news.write")
async def edit(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        post = await _post_or_404(s, request.path_params["slug"])
    kept = module.forms.recall(request)
    return {
        "page_title": f"Edit: {post.title}",
        "post": post,
        "title": kept.get("title") if kept else post.title,
        "category": kept.get("category") if kept else post.category,
        "categories": list(CATEGORIES.items()),
        "content": _kept_content(kept, post_content(post)).model_dump(),
        "published": post.status != "draft",
        "error": request.query_params.get("error"),
    }


# ---- actions ----------------------------------------------------------------------------


@module.router.post("/posts")
async def create_post(
    request: Request,
    who: CanWrite,
    title: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        async with module.session() as s:
            post = await service.create(
                s, who, title=title, category=category, content=_parse_content(content)
            )
            await s.commit()
            slug = post.slug
    except NewsError as exc:
        return await module.forms.back(request, "/news/new", str(exc))
    return _back(f"/news/p/{slug}")


@module.router.post("/posts/{slug}/save")
async def save_post(
    request: Request,
    slug: str,
    who: CanWrite,
    title: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        async with module.session() as s:
            post = await _post_or_404(s, slug)
            await service.save(
                s, who, post, title=title, category=category, content=_parse_content(content)
            )
            await s.commit()
            slug = post.slug  # a new title can mean a new slug
    except NewsError as exc:
        return await module.forms.back(request, f"/news/p/{slug}/edit", str(exc))
    return _back(f"/news/p/{slug}")


async def _lifecycle(slug: str, who: Principal, action: str) -> RedirectResponse:
    try:
        async with module.session() as s:
            post = await _post_or_404(s, slug)
            if action == "publish":
                await service.publish(s, who, post)
            elif action in ("pin", "unpin"):
                await service.set_pinned(s, who, post, action == "pin")
            elif action == "archive":
                await service.archive(s, who, post)
            elif action == "restore":
                await service.restore(s, who, post)
            elif action == "delete":
                await service.delete_draft(s, who, post)
                await s.commit()
                return _back("/news/")
            await s.commit()
    except NewsError as exc:
        return _back(f"/news/p/{slug}", str(exc))
    return _back(f"/news/p/{slug}")


@module.router.post("/posts/{slug}/publish")
async def publish_post(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "publish")


@module.router.post("/posts/{slug}/pin")
async def pin_post(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "pin")


@module.router.post("/posts/{slug}/unpin")
async def unpin_post(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "unpin")


@module.router.post("/posts/{slug}/archive")
async def archive_post(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "archive")


@module.router.post("/posts/{slug}/restore")
async def restore_post(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "restore")


@module.router.post("/posts/{slug}/delete")
async def delete_post(slug: str, who: CanPublish) -> RedirectResponse:
    return await _lifecycle(slug, who, "delete")
