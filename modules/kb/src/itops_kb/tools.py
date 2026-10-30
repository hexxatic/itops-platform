"""MCP tools: an assistant finds and reads how-tos to answer staff, and cites them. No write
tools: how-tos are written in Docs (docs_create_document)."""

from __future__ import annotations

from typing import Any, cast

from itops.sdk.content import Content, plain_text
from itops_kb import service
from itops_kb.app import module


class KbToolError(ValueError):
    """What the AI is told when a call can't be answered."""


@module.tool(permission="kb.read")
async def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search IT's how-tos for staff, best match first. Each result has the how-to's title
    and slug, where in it the match is, a snippet and a `url`. Answer people only from
    these how-tos, read in full with get_article(slug), and always give them the url. If
    nothing fits, say so and suggest contacting IT; don't make steps up."""
    hits = await module.search.query(query, scope="module", limit=max(1, min(limit, 20)))
    offered = {a["slug"] for a in await service.articles()}
    out: list[dict[str, Any]] = []
    for h in hits:
        if h.entity_id not in offered:  # e.g. in a collection kept out of AI
            continue
        d = h.as_dict()
        out.append({"title": h.title, "slug": h.entity_id, "where": d["where"],
                    "snippet": d["snippet"], "url": module.absolute_url(h.url)})  # fmt: skip
    return out


@module.tool(permission="kb.read")
async def get_article(slug: str) -> dict[str, Any]:
    """Read one how-to in full: its title, topic, when IT last updated it, and its steps as
    plain text. Give people its `url` with your answer."""
    a = await service.article(slug)
    if a is None:
        raise KbToolError(f"No how-to with slug {slug!r}. Use search to find one.")
    when = a.get("published_at")
    return {
        "title": a["title"],
        "slug": a["slug"],
        "topic": a["collection"]["name"],
        "updated": when.isoformat(timespec="minutes") + "Z" if when else None,
        "tags": a["tags"],
        "text": plain_text(cast(Content, a["content"])),
        "downloads": [f["filename"] for f in a["attachments"]],
        "url": module.absolute_url(f"/kb/a/{a['slug']}"),
    }
