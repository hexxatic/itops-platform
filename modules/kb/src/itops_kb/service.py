"""How-tos: reading them from Docs, keeping search in step, and the feedback (ADR 0027).

The how-tos are Docs' (published documents a publisher shows on the help site), read
through its ``howto_collections``, ``howtos`` and ``howto`` services as the person looking:
a how-to for an audience they're not in isn't there (ADR 0028). kb never copies or edits
them. Their images and attachments are Docs' files, which Docs opens to the people a how-to
is for. kb indexes them for the help site's search under its own module, from Docs'
``howto_index`` (every how-to, each with its audience's area, so search hides it like the
audience), and keeps which version it indexed (``kb.indexed``) so a sync only touches what
changed. Without Docs installed there are no how-tos.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Event, Principal
from itops.sdk.content import Content, passages
from itops_kb.app import module
from itops_kb.models import Feedback, Indexed, utcnow

KIND = "article"
COMMENT_MAX = 1000


class KbError(ValueError):
    """A user-facing problem (shown as a message, not a crash)."""


class NotFound(KbError):
    pass


# ---- reading Docs --------------------------------------------------------------------


async def _call(name: str, *args: Any) -> Any:
    fn = module.services.get(f"docs.{name}")
    return None if fn is None else await fn(*args)


async def collections() -> list[dict[str, Any]]:
    """``[{slug, name, description}]``, in Docs' order."""
    return cast(list[dict[str, Any]], await _call("howto_collections") or [])


async def articles(collection: str | None = None) -> list[dict[str, Any]]:
    """Every how-to (or one collection's), by title: Docs' ``howtos`` summaries."""
    return cast(list[dict[str, Any]], await _call("howtos", collection) or [])


async def article(slug: str) -> dict[str, Any] | None:
    """One how-to with its ``content`` and ``attachments``, or None."""
    return cast(dict[str, Any] | None, await _call("howto", slug))


def updated(a: dict[str, Any]) -> str:
    when = a.get("published_at")
    return f"{when:%d %B %Y}".lstrip("0") if isinstance(when, dt.datetime) else ""


# ---- search ---------------------------------------------------------------------------

_sync_lock = asyncio.Lock()  # one sync at a time (the job and Docs' events)


async def _to_index() -> list[dict[str, Any]]:
    """Every how-to, whatever its audience, with its content and area (Docs' howto_index)."""
    return cast(list[dict[str, Any]], await _call("howto_index") or [])


async def sync() -> str:
    """Index new and changed how-tos, remove the ones gone. Returns a summary. Whoever
    triggered it, it sees every how-to: each entry carries its audience's area."""
    async with _sync_lock:
        current = {a["slug"]: a for a in await _to_index()}
        async with module.session() as s:
            have = {r.slug: r for r in (await s.execute(select(Indexed))).scalars()}
        changed = [
            a for slug, a in current.items()
            if (r := have.get(slug)) is None or (r.version, r.title, r.collection_slug, r.area)
            != (a["version"], a["title"], a["collection"]["slug"], a.get("area"))
        ]  # fmt: skip
        gone = [slug for slug in have if slug not in current]
        for a in changed:  # one transaction per how-to (AGENTS.md: reindexers)
            async with module.session() as s:
                await module.search.index(
                    s, kind=KIND, entity_id=a["slug"], title=a["title"],
                    path=f"/a/{a['slug']}", passages=passages(cast(Content, a["content"])),
                    area=a.get("area"),
                )  # fmt: skip
                row = await s.get(Indexed, a["slug"]) or Indexed(slug=a["slug"])
                row.version, row.title = int(a["version"]), str(a["title"])
                row.collection_slug, row.area = str(a["collection"]["slug"]), a.get("area")
                row.indexed_at = utcnow()
                s.add(row)
                await s.commit()
        for slug in gone:
            async with module.session() as s:
                await module.search.remove(s, kind=KIND, entity_id=slug)
                await s.execute(delete(Indexed).where(Indexed.slug == slug))
                await s.commit()
    if not changed and not gone:
        return "Search is up to date."
    return f"Indexed {len(changed)} how-to(s), removed {len(gone)}."


@module.job("index")
async def index_job() -> str | None:
    return await sync()


@module.subscribe("docs.document_published")
@module.subscribe("docs.document_archived")
@module.subscribe("docs.collection_changed")
@module.subscribe("docs.help_site_changed")
async def docs_changed(_event: Event) -> None:
    await sync()


@module.search.reindexer
async def reindex() -> int:
    """Rebuild every how-to's search entries (``itops search rebuild``)."""
    async with module.session() as s:
        await s.execute(delete(Indexed))
        await s.commit()
    await sync()
    async with module.session() as s:
        return int((await s.execute(select(func.count()).select_from(Indexed))).scalar() or 0)


# ---- feedback -------------------------------------------------------------------------


async def leave_feedback(
    s: AsyncSession, who: Principal, slug: str, *, helpful: bool, comment: str = ""
) -> None:
    """A person says whether a how-to helped (one answer each; a new one replaces it).
    Their own action: never through AI."""
    if who.via_ai:
        raise KbError("Only the person themselves can say whether a how-to helped.")
    a = await article(slug)
    if a is None:
        raise NotFound("No such how-to.")
    comment = " ".join(comment.split())[:COMMENT_MAX]
    row = await my_feedback(s, who, slug)
    if row is None:
        row = Feedback(article_slug=slug, upn=who.user, helpful=helpful)
        s.add(row)
    row.helpful = helpful
    row.comment = comment or None
    row.created_at = utcnow()
    await module.audit.record(
        s, "feedback.left", kind=KIND, entity_id=slug, title=str(a["title"]),
        summary="Said it helped" if helpful else "Said it didn't help",
    )  # fmt: skip
    module.publish("feedback_left", {"slug": slug, "helpful": helpful})


async def my_feedback(s: AsyncSession, who: Principal, slug: str) -> Feedback | None:
    return (
        await s.execute(
            select(Feedback).where(Feedback.article_slug == slug, Feedback.upn == who.user)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class Answers:
    slug: str
    title: str | None  # None: no longer published
    helped: int
    total: int

    @property
    def not_helped(self) -> int:
        return self.total - self.helped


async def feedback_summary(s: AsyncSession, titles: dict[str, str]) -> list[Answers]:
    """Per how-to: helped / didn't, the least helpful first. ``titles``: the published ones."""
    helped = func.sum(case((Feedback.helpful == True, 1), else_=0))  # noqa: E712
    rows = await s.execute(
        select(Feedback.article_slug, func.count(), helped).group_by(Feedback.article_slug)
    )
    out = [
        Answers(slug=slug, title=titles.get(slug), helped=int(yes or 0), total=int(total))
        for slug, total, yes in rows.all()
    ]
    out.sort(key=lambda r: (r.helped / r.total, -r.total))
    return out


async def recent_comments(s: AsyncSession, limit: int = 30) -> list[Feedback]:
    rows = await s.execute(
        select(Feedback)
        .where(Feedback.comment.is_not(None))
        .order_by(Feedback.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())
