"""How-tos pages on the help site: find one, read it, say whether it helped; and, for IT,
what people said."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from itops.sdk import Principal
from itops.sdk.content import Content, render
from itops.sdk.content import toc as toc_of
from itops_kb import service
from itops_kb.app import module

CanRead = Annotated[Principal, Depends(module.require("kb.read"))]


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


def _row(a: dict[str, Any]) -> dict[str, Any]:
    return {"text": a["title"], "href": f"/kb/a/{a['slug']}", "note": a["excerpt"]}


@module.page("/", template="index.html", title="How-tos", nav="How-tos", permission="kb.read")
async def home(request: Request) -> dict[str, Any]:
    q = request.query_params.get("q", "").strip()
    chosen = request.query_params.get("collection") or None
    cols = await service.collections()
    shown = await service.articles(chosen)
    hits = await module.search.query(q, scope="module", limit=30) if q else []
    names = {c["slug"]: c["name"] for c in cols}
    groups: dict[str, list[dict[str, Any]]] = {}
    for a in shown:
        groups.setdefault(a["collection"]["slug"], []).append(_row(a))
    return {
        "query": q,
        "hits": hits,
        "chosen": names.get(chosen or "", "") if chosen else "",
        "collections": [
            {"text": c["name"], "href": f"/kb/?collection={c['slug']}", "note": c["description"]}
            for c in cols
        ],
        "groups": [
            {"name": names[slug], "articles": groups[slug]} for slug in names if slug in groups
        ],
        "can_review": _can("kb.feedback.manage"),
    }


@module.page("/a/{slug}", template="article.html", title="How-to", permission="kb.read")
async def article(request: Request) -> dict[str, Any]:
    slug = request.path_params["slug"]
    who = module.current_principal()
    a = await service.article(slug)
    if a is None:
        raise HTTPException(status_code=404, detail="We couldn't find that how-to.")
    async with module.session() as s:
        mine = await service.my_feedback(s, who, slug) if who else None
    content = cast(Content, a["content"])
    kept = module.forms.recall(request)
    return {
        "page_title": a["title"],
        "article": a,
        "updated": service.updated(a),
        "blocks": render(content),
        "toc": toc_of(content),
        "attachments": a["attachments"],
        "tags": a["tags"],
        "mine": mine,
        "comment": kept.get("comment", "") if kept else "",
        "thanks": request.query_params.get("thanks") == "1",
        "error": request.query_params.get("error"),
    }


@module.router.post("/feedback/{slug}")
async def leave_feedback(
    request: Request,
    slug: str,
    who: CanRead,
    helpful: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
) -> RedirectResponse:
    back = f"/kb/a/{slug}"
    if helpful not in ("yes", "no"):
        return await module.forms.back(request, back, "Choose Yes or No.")
    async with module.session() as s:
        try:
            await service.leave_feedback(s, who, slug, helpful=helpful == "yes", comment=comment)
        except service.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except service.KbError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        await s.commit()
    return RedirectResponse(f"{back}?thanks=1#feedback", status_code=303)


@module.page(
    "/feedback", template="feedback.html", title="What people said",
    permission="kb.feedback.manage",
)  # fmt: skip
async def feedback(request: Request) -> dict[str, Any]:
    titles = {a["slug"]: a["title"] for a in await service.articles()}
    async with module.session() as s:
        summary = await service.feedback_summary(s, titles)
        comments = await service.recent_comments(s)
    names = await module.people.names({c.upn for c in comments})
    return {
        "rows": [
            {
                "article": {"text": r.title, "href": f"/kb/a/{r.slug}"}
                if r.title
                else "No longer published",
                "helped": r.helped,
                "not_helped": r.not_helped,
                "share": f"{round(100 * r.helped / r.total)}%",
            }
            for r in summary
        ],
        "comments": [
            {
                "when": f"{c.created_at:%Y-%m-%d}",
                "article": {"text": titles[c.article_slug], "href": f"/kb/a/{c.article_slug}"}
                if c.article_slug in titles
                else "No longer published",
                "helped": {"badge": "Helped", "tone": "success"}
                if c.helpful
                else {"badge": "Didn't help", "tone": "warning"},
                "comment": c.comment or "",
                "who": names.get(c.upn, c.upn),
            }
            for c in comments
        ],
    }
