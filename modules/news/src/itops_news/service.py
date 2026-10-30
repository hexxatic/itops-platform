"""News service: the one place posts change.

Pages and AI tools both call these functions, so the rules live here once:

* A post is an **app update** (what changed in IT-Ops) or an **IT announcement** (news from
  the IT department).
* New posts are **drafts**: nobody but writers sees them until a person publishes
  (``news.publish``, human-only).
* Writers edit drafts. A **published** post is changed only by a person who may publish
  (corrections go live at once, so they're never AI's to make); pinning, archiving and
  deleting drafts are theirs too.
* Content is the core block format, normalized (sanitized) on every save. Published posts
  are in search; archived ones stay in, flagged.

Callers pass a session and commit.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal
from itops.sdk.content import Content, normalize, passages
from itops_news.app import module
from itops_news.models import Post, utcnow

CATEGORIES: dict[str, str] = {"app": "App update", "it": "IT announcement"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FILE_RE = re.compile(r"/files/([0-9a-fA-F-]{36})")


class NewsError(ValueError):
    """A user-facing problem (shown as a message, not a crash)."""


def slugify(title: str) -> str:
    return _SLUG_RE.sub("-", title.lower()).strip("-")[:180] or "post"


async def _unique_slug(s: AsyncSession, title: str, *, exclude: Any = None) -> str:
    base = slugify(title)
    slug, n = base, 1
    while True:
        q = select(Post.id).where(Post.slug == slug)
        if exclude is not None:
            q = q.where(Post.id != exclude)
        if (await s.execute(q)).first() is None:
            return slug
        n += 1
        slug = f"{base}-{n}"


def _clean(title: str, category: str, content: Content) -> tuple[str, str, Content]:
    title = " ".join(title.split())[:300]
    if not title:
        raise NewsError("Give the post a title.")
    if category not in CATEGORIES:
        raise NewsError(
            "Pick a kind of post: " + ", ".join(f"'{k}' ({v})" for k, v in CATEGORIES.items()) + "."
        )
    content = normalize(content)
    if not content.blocks:
        raise NewsError("The post is empty. Write something first.")
    return title, category, content


def _file_ids(content: Content) -> list[str]:
    ids: list[str] = []
    for b in content.blocks:
        ids += [m.lower() for m in _FILE_RE.findall(getattr(b, "html", ""))]
    return list(dict.fromkeys(ids))


def post_content(post: Post) -> Content:
    return Content.model_validate_json(post.content)


async def _audit(s: AsyncSession, post: Post, verb: str, summary: str) -> None:
    await module.audit.record(
        s, f"post.{verb}", kind="post", entity_id=str(post.id), title=post.title, summary=summary
    )


async def _index(s: AsyncSession, post: Post) -> None:
    """Published posts are searchable; archived ones stay in, flagged; drafts never."""
    if post.status == "draft":
        await module.search.remove(s, kind="post", entity_id=str(post.id))
        return
    await module.search.index(
        s,
        kind="post",
        entity_id=str(post.id),
        title=post.title,
        path=f"/p/{post.slug}",
        passages=passages(post_content(post)),
        archived=post.status == "archived",
    )


def _require_human(who: Principal) -> None:
    # news.publish is human-only (the real gate); this is a second lock.
    if who.via_ai:
        raise NewsError("Publishing, pinning, archiving and deleting posts is done by a person.")


# ---- reads ------------------------------------------------------------------------------


async def feed(
    s: AsyncSession, *, category: str | None = None, limit: int = 20, offset: int = 0
) -> list[Post]:
    """Published posts: pinned first, then newest."""
    q = (
        select(Post)
        .where(Post.status == "published")
        .order_by(Post.pinned.desc(), Post.published_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if category:
        q = q.where(Post.category == category)
    return list((await s.execute(q)).scalars())


async def drafts(s: AsyncSession) -> list[Post]:
    """Oldest first: publishing down the list leaves the newest post on top of the feed."""
    q = select(Post).where(Post.status == "draft").order_by(Post.created_at, Post.updated_at)
    return list((await s.execute(q)).scalars())


async def archived(s: AsyncSession, *, limit: int = 50) -> list[Post]:
    q = (
        select(Post)
        .where(Post.status == "archived")
        .order_by(Post.published_at.desc())
        .limit(limit)
    )
    return list((await s.execute(q)).scalars())


async def by_slug(s: AsyncSession, slug: str) -> Post | None:
    return (await s.execute(select(Post).where(Post.slug == slug))).scalar_one_or_none()


# ---- writes -----------------------------------------------------------------------------


async def create(
    s: AsyncSession, who: Principal, *, title: str, category: str, content: Content
) -> Post:
    """A new post, as a draft."""
    title, category, content = _clean(title, category, content)
    post = Post(
        slug=await _unique_slug(s, title),
        title=title,
        category=category,
        content=content.model_dump_json(),
        status="draft",
        created_by=who.user,
        created_via_ai=who.via_ai,
        updated_by=who.user,
    )
    s.add(post)
    await s.flush()
    await module.files.claim(s, _file_ids(content), entity_type="post", entity_id=str(post.id))
    await _audit(s, post, "created", "Drafted" + (" (via AI)" if who.via_ai else ""))
    return post


async def save(
    s: AsyncSession, who: Principal, post: Post, *, title: str, category: str, content: Content
) -> None:
    """Change a post. Drafts: anyone who may write. Published or archived: only a person
    who may publish (it changes what people already read)."""
    if post.status != "draft" and (who.via_ai or not who.has("news.publish")):
        raise NewsError(
            "This post is already published: only a person who can publish news changes "
            "it. Ask them, or write a new post."
        )
    title, category, content = _clean(title, category, content)
    if title != post.title:
        post.slug = await _unique_slug(s, title, exclude=post.id)
    post.title, post.category = title, category
    post.content = content.model_dump_json()
    post.updated_by, post.updated_at = who.user, utcnow()
    await s.flush()
    await module.files.claim(s, _file_ids(content), entity_type="post", entity_id=str(post.id))
    await _index(s, post)
    await _audit(
        s, post, "edited", "Edited" if post.status == "draft" else "Corrected the published post"
    )


def stamp(post: Post) -> str:
    """When the post last changed. The page sends it back with Publish or Discard draft,
    so a draft changed after the page loaded (an AI connection may) isn't acted on unread."""
    return post.updated_at.isoformat()


def _check_reviewed(post: Post, reviewed: str | None, done: str, verb: str) -> None:
    """``reviewed`` is the stamp the person's page showed; ``None`` only for callers
    without a page (tests)."""
    if reviewed is not None and reviewed != stamp(post):
        raise NewsError(
            f"Not {done}: {post.updated_by} changed this draft at "
            f"{post.updated_at:%Y-%m-%d %H:%M} UTC, after you opened it. It's shown below: "
            f"read it, then {verb} again."
        )


async def publish(s: AsyncSession, who: Principal, post: Post, *, reviewed: str | None) -> None:
    """Publish a draft: ``reviewed`` is :func:`stamp` as the person's page showed it."""
    _require_human(who)
    if post.status != "draft":
        raise NewsError("Only a draft can be published.")
    _check_reviewed(post, reviewed, "published", "publish")
    post.status = "published"
    post.published_by, post.published_at = who.user, utcnow()
    post.updated_by, post.updated_at = who.user, utcnow()
    await s.flush()
    await _index(s, post)
    await _audit(s, post, "published", "Published")
    module.publish(
        "post_published",
        {"id": str(post.id), "slug": post.slug, "title": post.title, "category": post.category},
    )


async def set_pinned(s: AsyncSession, who: Principal, post: Post, pinned: bool) -> None:
    """Pinned posts stay at the top of News."""
    _require_human(who)
    if post.status != "published":
        raise NewsError("Only a published post can be pinned.")
    if post.pinned != pinned:
        post.pinned = pinned
        await _audit(
            s, post, "pinned" if pinned else "unpinned", "Pinned" if pinned else "Unpinned"
        )


async def archive(s: AsyncSession, who: Principal, post: Post) -> None:
    """Take a published post off the news page. Nothing is deleted; it can be restored."""
    _require_human(who)
    if post.status != "published":
        raise NewsError("Only a published post can be archived.")
    post.status, post.pinned = "archived", False
    post.updated_by, post.updated_at = who.user, utcnow()
    await _index(s, post)
    await _audit(s, post, "archived", "Archived")


async def restore(s: AsyncSession, who: Principal, post: Post) -> None:
    _require_human(who)
    if post.status != "archived":
        raise NewsError("Only an archived post can be restored.")
    post.status = "published"
    post.updated_by, post.updated_at = who.user, utcnow()
    await _index(s, post)
    await _audit(s, post, "restored", "Restored from the archive")


async def delete_draft(
    s: AsyncSession, who: Principal, post: Post, *, reviewed: str | None
) -> None:
    """Throw away a draft that was never published (published posts are archived)."""
    _require_human(who)
    if post.status != "draft":
        raise NewsError("Only a draft can be deleted; archive a published post instead.")
    _check_reviewed(post, reviewed, "discarded", "discard it")
    await _audit(s, post, "deleted", "Deleted the draft")
    await module.search.remove(s, kind="post", entity_id=str(post.id))
    await s.delete(post)


@module.search.reindexer
async def reindex_all() -> int:
    """Rebuild every post's search entry (``itops search rebuild``)."""
    count = 0
    async with module.session() as s:
        for post in list((await s.execute(select(Post))).scalars()):
            await _index(s, post)
            await s.commit()  # one at a time: never one long transaction
            count += post.status != "draft"
        await s.commit()
    return count


@module.service("waiting")
async def waiting() -> list[dict[str, Any]]:
    """What's waiting for the person looking (core's *Waiting for you*): drafts to publish."""
    who = module.current_principal()
    if who is None or not who.has("news.publish"):
        return []
    async with module.session() as s:
        count = len(await drafts(s))
    return [{"title": f"{count} news draft{'s' if count != 1 else ''} to publish",
             "url": "/news/", "count": count}]  # fmt: skip
