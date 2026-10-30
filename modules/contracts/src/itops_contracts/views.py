"""Contracts pages (server-rendered, core components only) and the form actions behind them.

Pages live at /contracts/...; actions are POSTs under /api/contracts/... that redirect back
to a page (plain forms, no JavaScript needed). Problems come back as ``?error=...`` and a
failed save keeps the form (``module.forms``).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from itops.sdk import AIUnavailable, FileRejected, KeptForm, Principal
from itops_contracts import service
from itops_contracts.app import module
from itops_contracts.models import Contract, Proposal, Term
from itops_contracts.service import (
    CATEGORIES,
    COST_PERIODS,
    LINKS,
    PROPOSAL_STATES,
    STATES,
    ContractInput,
    ContractsError,
    ParsedRate,
    RateInput,
    TermInput,
    can_see_costs,
    cancel_by,
    contract_links,
    term_attachments,
    yearly,
)

CanWrite = Annotated[Principal, Depends(module.require("contracts.write"))]
CanApprove = Annotated[Principal, Depends(module.require("contracts.approve"))]

STATE_TONE = {
    "active": "success",
    "due": "warning",
    "negotiating": "info",
    "renewed": "neutral",
    "cancelled": "neutral",
    "lapsed": "danger",
}
Uploads = Annotated[list[UploadFile] | None, File()]


def _when(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _money(value: Decimal | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def _price(value: Decimal) -> str:
    """A rate's price to as many places as it has, at least two: $0.0099, $0.069, $1.25."""
    whole, _, frac = f"{value:,.5f}".rstrip("0").partition(".")
    return f"${whole}.{frac.ljust(2, '0')}"


def _rate_text(r: ParsedRate | None) -> str:
    if r is None:
        return ""
    return f"{_price(r.price)} each" + (f", {r.included:,} a month included" if r.included else "")


def _parsed(rates: list[Any]) -> list[ParsedRate]:
    return [ParsedRate(r.item, r.price, r.included_per_month) for r in rates]


def _rate_rows(rates: list[ParsedRate]) -> list[dict[str, str]]:
    return [{"item": r.item, "price": _price(r.price),
             "included": f"{r.included:,}" if r.included else ""} for r in rates]  # fmt: skip


async def _proposal_view(s: Any, p: Proposal, t: Term, c: Contract) -> dict[str, Any]:
    """A proposal as its card shows it: each thing it changes, now and proposed."""
    data = service.proposal_data(p)
    rows: list[dict[str, str]] = []
    if "cost" in data:
        now = f"{_money(t.cost)} {COST_PERIODS[t.cost_period]}" if t.cost is not None else ""
        rows.append({"field": "Cost", "before": now or "Not set",
                     "after": f"{_money(Decimal(data['cost']))} "
                              f"{COST_PERIODS[data['cost_period']]}"})  # fmt: skip
    given = service.proposal_rates(p)
    if given is not None:
        now_rates = {r.item: r for r in _parsed(await service.rates(s, t.id))}
        new_rates = {r.item: r for r in given}
        for item in [*new_rates, *(i for i in now_rates if i not in new_rates)]:
            rows.append({"field": item, "before": _rate_text(now_rates.get(item)) or "Not set",
                         "after": _rate_text(new_rates.get(item)) or "Removed"})  # fmt: skip
    return {
        "id": p.id,
        "contract": {"text": c.name, "href": f"/contracts/c/{c.id}"},
        "title": f"{c.name}: {service.proposal_what(p)}",
        "term": f"{t.starts_on} to {t.ends_on}",
        "by": p.proposed_by + (" (via AI)" if p.proposed_via_ai else ""),
        "when": _when(p.proposed_at),
        "note": p.note,
        "rows": rows,
        "status": PROPOSAL_STATES[p.status],
        "decided": f"{p.decided_by}, {_when(p.decided_at)}" if p.decided_by else "",
    }


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


def _back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


async def _contract_or_404(s: Any, contract_id: str) -> Contract:
    c = await service.get(s, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    return c


def _left(days: int) -> dict[str, str]:
    """Days until cancel-by as a badge."""
    if days < 0:
        return {"badge": f"{-days} days overdue", "tone": "danger"}
    if days <= 30:
        return {"badge": f"{days} days left", "tone": "danger"}
    if days <= 60:
        return {"badge": f"{days} days left", "tone": "warning"}
    return {"badge": f"{days} days left", "tone": "info"}


def _state(t: Term | None) -> dict[str, str] | str:
    if t is None:
        return ""
    return {"badge": STATES[t.state], "tone": STATE_TONE[t.state]}


def _seats(t: Term) -> str:
    if t.seats_bought is None and t.seats_in_use is None:
        return ""
    used = "?" if t.seats_in_use is None else str(t.seats_in_use)
    bought = "?" if t.seats_bought is None else str(t.seats_bought)
    return f"{used} of {bought} in use"


def _seat_warning(t: Term) -> str | None:
    if t.seats_bought is None or t.seats_in_use is None or t.seats_bought == 0:
        return None
    if t.seats_in_use > t.seats_bought:
        return (f"{t.seats_in_use - t.seats_bought} more seats in use than bought: expect a "
                "true-up charge, or a finding at an audit. Buy the extra seats or remove "
                "users, then update Seats bought on this term (Edit term).")  # fmt: skip
    if t.seats_in_use < t.seats_bought * 0.8:
        return (f"{t.seats_bought - t.seats_in_use} seats bought but not in use: paid for and "
                "unused. Lower the count at the next renewal, or update Seats in use if it's "
                "out of date.")  # fmt: skip
    return None


def _kept_bool(kept: KeptForm | None, name: str, fallback: bool) -> bool:
    return bool(kept.get(name)) if kept is not None else fallback


async def _form_links(request: Request) -> dict[str, Any]:
    form = await request.form()
    out: dict[str, Any] = {}
    for f in LINKS.fields:
        values = [str(v) for v in form.getlist(f"f__{f.key}") if str(v).strip()]
        out[f.key] = values if f.multiple else (values[0] if values else "")
    return out


async def _store_uploads(files: list[UploadFile] | None) -> list[dict[str, Any]]:
    """Save uploaded contract files in the core file store (claimed when the term saves)."""
    out: list[dict[str, Any]] = []
    limit = module.files.max_bytes(attachment=True)
    for f in files or []:
        if not f.filename:
            continue  # an empty file input still sends a part
        data = await f.read(limit + 1)
        try:
            stored = await module.files.save(
                data, mime_type=f.content_type or "", filename=f.filename, attachment=True
            )
        except FileRejected as exc:
            raise ContractsError(str(exc)) from exc
        out.append(
            {
                "id": stored.id,
                "filename": stored.filename,
                "size": stored.size_bytes,
                "mime": stored.mime_type,
            }
        )
    return out


def _contract_input(
    name: str,
    category: str,
    vendor: str,
    owner: str,
    description: str,
    auto_renews: str,
    notice_days: str,
    links: dict[str, Any],
) -> ContractInput:
    return ContractInput(
        name=name,
        category=category,
        vendor=vendor,
        owner_upn=owner or None,
        description=description,
        auto_renews=auto_renews == "1",
        notice_days=notice_days,
        links=links,
    )


async def _form_context(kept: KeptForm | None, c: Contract | None) -> dict[str, Any]:
    """What the contract form needs: choices, and values from a kept form or the contract."""
    values = contract_links(c) if c else {}
    link_options = await module.links.options(LINKS, keep=values or None)
    owners = [("", "Nobody yet")] + await module.people.options(holding="contracts.read")

    def pick(name: str, fallback: Any) -> Any:
        return kept.get(name) if kept is not None else fallback

    return {
        "categories": list(CATEGORIES.items()),
        "periods": list(COST_PERIODS.items()),
        "owners": owners,
        "link_definition": LINKS,
        "link_options": link_options,
        "links": kept.fields() if kept is not None else values,
        "name": pick("name", c.name if c else ""),
        "category": pick("category", c.category if c else "software"),
        "vendor": pick("vendor", c.vendor if c else ""),
        "owner": pick("owner", (c.owner_upn or "") if c else ""),
        "description": pick("description", (c.description or "") if c else ""),
        "auto_renews": _kept_bool(kept, "auto_renews", c.auto_renews if c else False),
        "notice_days": pick("notice_days", str(c.notice_days) if c else "30"),
        "can_costs": can_see_costs(module.current_principal()),
    }


def _term_context(kept: KeptForm | None, t: Term | None) -> dict[str, Any]:
    def pick(name: str, fallback: Any) -> Any:
        return kept.get(name) if kept is not None else fallback

    return {
        "starts_on": pick("starts_on", str(t.starts_on) if t else ""),
        "ends_on": pick("ends_on", str(t.ends_on) if t else ""),
        "cost": pick("cost", "" if t is None or t.cost is None else f"{t.cost:.2f}"),
        "cost_period": pick("cost_period", t.cost_period if t else "year"),
        "po_number": pick("po_number", t.po_number if t else ""),
        "seats_bought": pick(
            "seats_bought", "" if t is None or t.seats_bought is None else str(t.seats_bought)
        ),
        "seats_in_use": pick(
            "seats_in_use", "" if t is None or t.seats_in_use is None else str(t.seats_in_use)
        ),
    }


# ---- pages --------------------------------------------------------------------------------


@module.page(
    "/", template="index.html", title="Contracts", nav="Contracts", permission="contracts.read"
)
async def home(request: Request) -> dict[str, Any]:
    category = request.query_params.get("category") or None
    owner = request.query_params.get("owner") or None
    archived = request.query_params.get("archived") == "1"
    query = request.query_params.get("q", "")
    async with module.session() as s:
        coming = await service.upcoming(s)
        found = await service.contracts(
            s,
            status="archived" if archived else "active",
            category=category,
            owner=owner,
            query=query,
        )
        current = {c.id: await service.current_term(s, c.id) for c in found}
        drafts = await service.contracts(s, status="draft") if _can("contracts.write") else []
        costs = can_see_costs(module.current_principal())
        proposed = len(await service.proposals(s)) if costs else 0
    # Files this person had read for a new contract, not yet made into one or discarded.
    files = [
        r for r in await module.ai.my_runs_to_review() if r.entity_type == NEW_ENTITY
    ] if _can("contracts.write") else []  # fmt: skip
    names = await module.people.names(
        [c.owner_upn for c in [*found, *(u.contract for u in coming)] if c.owner_upn]
    )
    rows: list[dict[str, Any]] = []
    for c in found:
        t = current[c.id]
        rows.append(
            {
                "name": {"text": c.name, "href": f"/contracts/c/{c.id}"},
                "vendor": c.vendor,
                "category": CATEGORIES.get(c.category, c.category),
                "owner": names.get(c.owner_upn or "", c.owner_upn or ""),
                "ends": str(t.ends_on) if t else "",
                "cancel_by": str(cancel_by(c, t)) if t else "",
                "state": _state(t),
            }
        )
    return {
        "upcoming": [
            {
                "name": {"text": u.contract.name, "href": f"/contracts/c/{u.contract.id}"},
                "vendor": u.contract.vendor,
                "owner": names.get(u.contract.owner_upn or "", u.contract.owner_upn or ""),
                "cancel_by": str(u.cancel_by),
                "left": _left(u.days_left),
                "renews": "Auto-renews" if u.contract.auto_renews else "Ends",
                "state": _state(u.term),
            }
            for u in coming
        ],
        "rows": rows,
        "drafts": [
            {
                "name": {"text": c.name, "href": f"/contracts/c/{c.id}"},
                "vendor": c.vendor,
                "by": c.created_by,
                "when": _when(c.created_at),
            }
            for c in drafts
        ],
        "files": [
            {
                "file": {"text": r.filename, "href": f"/contracts/from-file/{r.id}"},
                "when": _when(r.finished_at or r.created_at),
            }
            for r in files
        ],
        "categories": [("", "All categories"), *CATEGORIES.items()],
        "owners": [("", "Anyone"), *await module.people.options(holding="contracts.read")],
        "category": category or "",
        "owner": owner or "",
        "archived": archived,
        "query": query,
        "can_write": _can("contracts.write"),
        "can_approve": _can("contracts.approve"),
        "can_costs": costs,
        "proposed": proposed,
        "error": request.query_params.get("error"),
    }


@module.page(
    "/c/{contract_id}", template="contract.html", title="Contract", permission="contracts.read"
)
async def contract_page(request: Request) -> dict[str, Any]:
    who = module.current_principal()
    costs = can_see_costs(who)
    async with module.session() as s:
        c = await _contract_or_404(s, request.path_params["contract_id"])
        history = await service.terms(s, c.id)
        rates = _parsed(await service.rates(s, history[0].id)) if costs and history else []
        proposed = [await _proposal_view(s, *x) for x in await service.proposals(
            s, contract_id=c.id)] if costs else []  # fmt: skip
    t = history[0] if history else None
    links = await module.links.resolve(contract_links(c))
    owner = (await module.people.names([c.owner_upn])).get(c.owner_upn) if c.owner_upn else None
    left = (cancel_by(c, t) - service.today()).days if t else None
    return {
        "page_title": c.name,
        "contract": c,
        "stamp": service.stamp(c),
        "category": CATEGORIES.get(c.category, c.category),
        "owner": owner or c.owner_upn or "Nobody yet",
        "term": t,
        "term_state": _state(t),
        "cancel_by": str(cancel_by(c, t)) if t else "",
        "left": _left(left)
        if t is not None and left is not None and t.state in service.OPEN_STATES
        else None,
        "cost": f"{_money(t.cost)} {COST_PERIODS[t.cost_period]}"
        if t and costs and t.cost is not None
        else "",
        "seats": _seats(t) if t else "",
        "can_costs": costs,
        "rates": _rate_rows(rates),
        "proposals": proposed,
        "seat_warning": _seat_warning(t) if t else None,
        "files": term_attachments(t) if t else [],
        "can_suggest": _suggestions_on() and _can("contracts.write") and c.status != "archived",
        "to_review": [
            r
            for r in await module.ai.suggestion_runs("term", str(t.id))
            if r.status in ("queued", "running", "done")
        ]
        if t
        else [],
        "link_definition": LINKS,
        "link_values": contract_links(c),
        "links": links,
        "history": [
            {
                "term": f"{h.starts_on} to {h.ends_on}",
                "state": _state(h),
                **(
                    {
                        "cost": f"{_money(h.cost)} {COST_PERIODS[h.cost_period]}"
                        if h.cost is not None
                        else ""
                    }
                    if costs
                    else {}
                ),
                "po": h.po_number,
                "decision": " ".join(x for x in [h.decided_by or "", h.decision_note] if x),
                "edit": {"text": "Edit", "href": f"/contracts/t/{h.id}/edit"}
                if _can("contracts.write") and c.status != "archived"
                else "",
            }
            for h in history
        ],
        "history_columns": [
            ("term", "Term"),
            ("state", "State"),
            *([("cost", "Cost")] if costs else []),
            ("po", "PO"),
            ("decision", "Decided"),
            ("edit", ""),
        ],
        "activity": await module.audit.history(kind="contract", entity_id=str(c.id)),
        "can_write": _can("contracts.write") and c.status != "archived",
        "can_approve": _can("contracts.approve"),
        "undecided": t is not None and t.state in service.OPEN_STATES and c.status == "active",
        "error": request.query_params.get("error"),
    }


@module.page("/new", template="edit.html", title="New contract", permission="contracts.write")
async def new_page(request: Request) -> dict[str, Any]:
    kept = module.forms.recall(request)
    return {
        "mode": "new",
        **await _form_context(kept, None),
        **_term_context(kept, None),
        "can_suggest": _suggestions_on(),
        "max_attachment_mb": module.files.max_bytes(attachment=True) // (1024 * 1024),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/c/{contract_id}/edit",
    template="edit.html",
    title="Edit contract",
    permission="contracts.write",
)
async def edit_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        c = await _contract_or_404(s, request.path_params["contract_id"])
    kept = module.forms.recall(request)
    return {
        "page_title": f"Edit: {c.name}",
        "mode": "edit",
        "contract": c,
        **await _form_context(kept, c),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/t/{term_id}/edit", template="term_edit.html", title="Edit term", permission="contracts.write"
)
async def term_page(request: Request) -> dict[str, Any]:
    costs = can_see_costs(module.current_principal())
    async with module.session() as s:
        t = await service.get_term(s, request.path_params["term_id"])
        if t is None:
            raise HTTPException(status_code=404, detail="Term not found")
        c = await _contract_or_404(s, str(t.contract_id))
        saved = _parsed(await service.rates(s, t.id)) if costs else []
    kept = module.forms.recall(request)
    if kept is not None:
        rate_rows = [{"item": i, "price": p, "included": n} for i, p, n in zip(
            kept.getlist("rate_item"), kept.getlist("rate_price"), kept.getlist("rate_included"),
            strict=False)]  # fmt: skip
    else:
        rate_rows = [
            {"item": r.item, "price": f"{r.price.normalize():f}",
             "included": "" if r.included is None else str(r.included)}
            for r in saved
        ]  # fmt: skip
    return {
        "page_title": f"Term: {c.name}",
        "contract": c,
        "term": t,
        "rate_rows": rate_rows + [{"item": "", "price": "", "included": ""}] * 3,
        **_term_context(kept, t),
        "periods": list(COST_PERIODS.items()),
        "files": term_attachments(t),
        "files_dropped": bool(kept and kept.files_dropped),
        "max_attachment_mb": module.files.max_bytes(attachment=True) // (1024 * 1024),
        "can_costs": costs,
        "error": request.query_params.get("error"),
    }


@module.page(
    "/c/{contract_id}/renew", template="renew.html", title="Renew", permission="contracts.approve"
)
async def renew_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        c = await _contract_or_404(s, request.path_params["contract_id"])
        t = await service.current_term(s, c.id)
    if t is None:
        raise HTTPException(status_code=404, detail="Contract has no term")
    length = t.ends_on - t.starts_on
    proposed = Term(
        starts_on=t.ends_on + dt.timedelta(days=1),
        ends_on=t.ends_on + length,  # same length, same end date next time
        cost=t.cost,
        cost_period=t.cost_period,
        po_number="",
        seats_bought=t.seats_bought,
        seats_in_use=t.seats_in_use,
    )
    kept = module.forms.recall(request)
    return {
        "page_title": f"Renew: {c.name}",
        "contract": c,
        "term": t,
        "cancel_by": str(cancel_by(c, t)),
        **_term_context(kept, proposed),
        "note": kept.get("note") if kept else "",
        "periods": list(COST_PERIODS.items()),
        "can_costs": can_see_costs(module.current_principal()),
        "error": request.query_params.get("error"),
    }


@module.page("/costs", template="costs.html", title="Contract costs", permission="contracts.costs")
async def costs_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        active = await service.contracts(s)
        current = {c.id: await service.current_term(s, c.id) for c in active}
    by_category: dict[str, Decimal] = defaultdict(Decimal)
    by_vendor: dict[str, Decimal] = defaultdict(Decimal)
    rows: list[dict[str, Any]] = []
    total, one_off = Decimal(0), Decimal(0)
    for c in active:
        t = current[c.id]
        if t is None or t.cost is None:
            continue
        per_year = yearly(t)
        if per_year is None:
            one_off += t.cost
        else:
            total += per_year
            by_category[CATEGORIES.get(c.category, c.category)] += per_year
            by_vendor[c.vendor or "(no vendor)"] += per_year
        rows.append(
            {
                "name": {"text": c.name, "href": f"/contracts/c/{c.id}"},
                "vendor": c.vendor,
                "cost": f"{_money(t.cost)} {COST_PERIODS[t.cost_period]}",
                "yearly": _money(per_year),
                "term": f"{t.starts_on} to {t.ends_on}",
            }
        )

    def ranked(totals: dict[str, Decimal]) -> list[dict[str, str]]:
        return [
            {"name": k, "yearly": _money(v)}
            for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
        ]

    return {
        "total": _money(total),
        "one_off": _money(one_off),
        "rows": sorted(rows, key=lambda r: r["name"]["text"]),
        "by_category": ranked(by_category),
        "by_vendor": ranked(by_vendor),
    }


@module.page("/proposals", template="proposals.html", title="Proposed costs",
             permission="contracts.costs")  # fmt: skip
async def proposals_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        waiting = [await _proposal_view(s, *x) for x in await service.proposals(s)]
        decided = [
            await _proposal_view(s, *x)
            for x in await service.proposals(s, status=None, limit=50)
            if x[0].status != "pending"
        ]
    return {
        "proposals": waiting,
        "decided": [{**d, "what": d["title"]} for d in decided],
        "error": request.query_params.get("error"),
    }


# ---- actions ------------------------------------------------------------------------------

Text = Annotated[str, Form()]
Texts = Annotated[list[str] | None, Form()]


@module.router.post("/contracts")
async def create_contract(
    request: Request,
    who: CanWrite,
    name: Text = "",
    category: Text = "software",
    vendor: Text = "",
    owner: Text = "",
    description: Text = "",
    auto_renews: Text = "",
    notice_days: Text = "0",
    starts_on: Text = "",
    ends_on: Text = "",
    cost: Text = "",
    cost_period: Text = "year",
    po_number: Text = "",
    seats_bought: Text = "",
    seats_in_use: Text = "",
    attachments: Uploads = None,
) -> RedirectResponse:
    data = _contract_input(
        name,
        category,
        vendor,
        owner,
        description,
        auto_renews,
        notice_days,
        await _form_links(request),
    )
    term = TermInput(starts_on, ends_on, cost, cost_period, po_number, seats_bought, seats_in_use)
    try:
        files = await _store_uploads(attachments)
        async with module.session() as s:
            c = await service.create(s, who, data, term, files=files)
            await s.commit()
            cid = c.id
    except ContractsError as exc:
        return await module.forms.back(request, "/contracts/new", str(exc))
    return _back(f"/contracts/c/{cid}")


@module.router.post("/contracts/{contract_id}")
async def update_contract(
    request: Request,
    contract_id: str,
    who: CanWrite,
    name: Text = "",
    category: Text = "software",
    vendor: Text = "",
    owner: Text = "",
    description: Text = "",
    auto_renews: Text = "",
    notice_days: Text = "0",
) -> RedirectResponse:
    data = _contract_input(
        name,
        category,
        vendor,
        owner,
        description,
        auto_renews,
        notice_days,
        await _form_links(request),
    )
    try:
        async with module.session() as s:
            c = await _contract_or_404(s, contract_id)
            await service.update(s, who, c, data)
            await s.commit()
    except ContractsError as exc:
        return await module.forms.back(request, f"/contracts/c/{contract_id}/edit", str(exc))
    return _back(f"/contracts/c/{contract_id}")


@module.router.post("/terms/{term_id}")
async def update_term(
    request: Request,
    term_id: str,
    who: CanWrite,
    starts_on: Text = "",
    ends_on: Text = "",
    cost: Text = "",
    cost_period: Text = "year",
    po_number: Text = "",
    seats_bought: Text = "",
    seats_in_use: Text = "",
    attachments: Uploads = None,
    remove_attachment: Annotated[list[str] | None, Form()] = None,
    rate_item: Texts = None,
    rate_price: Texts = None,
    rate_included: Texts = None,
) -> RedirectResponse:
    term = TermInput(starts_on, ends_on, cost, cost_period, po_number, seats_bought, seats_in_use)
    rates = [RateInput(i, p, n) for i, p, n in zip(
        rate_item or [], rate_price or [], rate_included or [], strict=False)]  # fmt: skip
    contract_id = ""
    try:
        uploaded = await _store_uploads(attachments)
        async with module.session() as s:
            t = await service.get_term(s, term_id)
            if t is None:
                raise HTTPException(status_code=404, detail="Term not found")
            c = await _contract_or_404(s, str(t.contract_id))
            contract_id = str(c.id)
            await service.update_term(
                s, who, c, t, term, add_files=uploaded, remove_files=set(remove_attachment or [])
            )
            if can_see_costs(who):  # the form shows rates only to them; others keep them
                await service.set_rates(s, who, c, t, rates)
            await s.commit()
    except ContractsError as exc:
        return await module.forms.back(request, f"/contracts/t/{term_id}/edit", str(exc))
    return _back(f"/contracts/c/{contract_id}")


@module.router.post("/contracts/{contract_id}/renew")
async def renew_contract(
    request: Request,
    contract_id: str,
    who: CanApprove,
    starts_on: Text = "",
    ends_on: Text = "",
    cost: Text = "",
    cost_period: Text = "year",
    po_number: Text = "",
    seats_bought: Text = "",
    seats_in_use: Text = "",
    note: Text = "",
) -> RedirectResponse:
    term = TermInput(starts_on, ends_on, cost, cost_period, po_number, seats_bought, seats_in_use)
    try:
        async with module.session() as s:
            c = await _contract_or_404(s, contract_id)
            await service.renew(s, who, c, term, note=note)
            await s.commit()
    except ContractsError as exc:
        return await module.forms.back(request, f"/contracts/c/{contract_id}/renew", str(exc))
    return _back(f"/contracts/c/{contract_id}")


async def _decide(
    contract_id: str, who: Principal, action: str, note: str = "", *, reviewed: str | None = None
) -> RedirectResponse:
    try:
        async with module.session() as s:
            c = await _contract_or_404(s, contract_id)
            if action == "negotiate":
                await service.start_negotiating(s, who, c)
            elif action == "cancel":
                await service.cancel(s, who, c, note=note)
            elif action == "accept":
                await service.accept(s, who, c, reviewed=reviewed)
            elif action == "archive":
                await service.archive(s, who, c)
            elif action == "restore":
                await service.restore(s, who, c)
            elif action == "discard":
                await service.discard_draft(s, who, c, reviewed=reviewed)
                await s.commit()
                return _back("/contracts/")
            await s.commit()
    except ContractsError as exc:
        return _back(f"/contracts/c/{contract_id}", str(exc))
    return _back(f"/contracts/c/{contract_id}")


@module.router.post("/contracts/{contract_id}/negotiate")
async def negotiate(contract_id: str, who: CanWrite) -> RedirectResponse:
    return await _decide(contract_id, who, "negotiate")


@module.router.post("/contracts/{contract_id}/cancel")
async def cancel_contract(contract_id: str, who: CanApprove, note: Text = "") -> RedirectResponse:
    return await _decide(contract_id, who, "cancel", note)


# Accept and discard send back the stamp the page showed (``?reviewed=``), so a draft
# changed after the page loaded (by an AI connection, say) is refused, not acted on.
@module.router.post("/contracts/{contract_id}/accept")
async def accept_draft(contract_id: str, who: CanApprove, reviewed: str) -> RedirectResponse:
    return await _decide(contract_id, who, "accept", reviewed=reviewed)


@module.router.post("/contracts/{contract_id}/discard")
async def discard_draft(contract_id: str, who: CanApprove, reviewed: str) -> RedirectResponse:
    return await _decide(contract_id, who, "discard", reviewed=reviewed)


@module.router.post("/contracts/{contract_id}/archive")
async def archive_contract(contract_id: str, who: CanApprove) -> RedirectResponse:
    return await _decide(contract_id, who, "archive")


@module.router.post("/contracts/{contract_id}/restore")
async def restore_contract(contract_id: str, who: CanApprove) -> RedirectResponse:
    return await _decide(contract_id, who, "restore")


async def _decide_proposal(proposal_id: int, who: Principal, *, apply: bool,
                           back: str) -> RedirectResponse:  # fmt: skip
    """Apply or reject; back to the proposals page (``back=list``) or the contract's page."""
    url = "/contracts/proposals"
    try:
        async with module.session() as s:
            p = await service.get_proposal(s, proposal_id)
            if p is None:
                raise HTTPException(status_code=404, detail="Proposal not found")
            c = await service.decide_proposal(s, who, p, apply=apply)
            await s.commit()
            if back != "list":
                url = f"/contracts/c/{c.id}"
    except ContractsError as exc:
        return _back(url, str(exc))
    return _back(url)


@module.router.post("/proposals/{proposal_id}/apply")
async def apply_proposal(proposal_id: int, who: CanWrite, back: str = "") -> RedirectResponse:
    return await _decide_proposal(proposal_id, who, apply=True, back=back)


@module.router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: int, who: CanWrite, back: str = "") -> RedirectResponse:
    return await _decide_proposal(proposal_id, who, apply=False, back=back)


# ---- AI suggestions from a term's files (ADR 0015) -------------------------------------------


def _suggestions_on() -> bool:
    return module.ai.can_suggest(service.SUGGEST_FEATURE)


async def _term_and_contract(s: Any, term_id: str) -> tuple[Term, Contract]:
    t = await service.get_term(s, term_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Term not found")
    return t, await _contract_or_404(s, str(t.contract_id))


@module.router.post("/terms/{term_id}/suggest/{file_id}")
async def suggest_from_file(term_id: str, file_id: str, who: CanWrite) -> RedirectResponse:
    async with module.session() as s:
        t, c = await _term_and_contract(s, term_id)
    if who.via_ai:
        return _back(f"/contracts/c/{c.id}", "Suggestions are started and reviewed by a person.")
    if file_id not in {str(a["id"]) for a in term_attachments(t)}:
        raise HTTPException(status_code=404, detail="That file isn't on this term")
    try:
        run_id = await module.ai.suggest(
            service.SUGGEST_FEATURE, file_id,
            service.suggest_fields(costs=can_see_costs(who)),
            entity=("term", str(t.id)), instructions=service.SUGGEST_INSTRUCTIONS,
        )  # fmt: skip
    except AIUnavailable as exc:
        return _back(f"/contracts/c/{c.id}", str(exc))
    return _back(f"/contracts/t/{t.id}/suggestions/{run_id}")


@module.page("/t/{term_id}/suggestions/{run_id}", template="suggestions.html",
             title="Suggestions", permission="contracts.write")  # fmt: skip
async def suggestions_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        t, c = await _term_and_contract(s, request.path_params["term_id"])
    run = await module.ai.suggestion_run(int(request.path_params["run_id"]))
    if run is None or run.entity_id != str(t.id):
        raise HTTPException(status_code=404, detail="Suggestions not found")
    costs = can_see_costs(module.current_principal())
    current: dict[str, Any] = {
        "starts_on": str(t.starts_on), "ends_on": str(t.ends_on),
        "cost": _money(t.cost) if costs else "", "cost_period": COST_PERIODS.get(t.cost_period, ""),
        "po_number": t.po_number, "seats_bought": t.seats_bought, "notice_days": c.notice_days,
        "auto_renews": "Yes" if c.auto_renews else "No",
    }  # fmt: skip
    by_key = {x.key: x for x in run.suggestions}
    rows = [
        {"field": f, "suggestion": by_key[f.key], "current": current.get(f.key, "")}
        for f in run.fields
        if f.key in by_key and (costs or f.key not in ("cost", "cost_period"))
    ]
    pages = await module.files.text(run.file_id) if run.status in ("done", "reviewed") else []
    return {
        "page_title": f"Suggestions: {c.name}",
        "contract": c,
        "term": t,
        "run": run,
        "rows": rows,
        "file": {"id": run.file_id, "filename": run.filename},
        "pages": pages,
        "periods": list(COST_PERIODS.items()),
        "error": request.query_params.get("error"),
    }


@module.router.post("/terms/{term_id}/suggestions/{run_id}")
async def accept_suggestions(
    request: Request, term_id: str, run_id: int, who: CanWrite
) -> RedirectResponse:
    run = await module.ai.suggestion_run(run_id)
    if run is None or run.entity_id != term_id or run.status != "done":
        raise HTTPException(status_code=404, detail="Nothing to review")
    form = await request.form()
    values: dict[str, Any] = {}
    for f in run.fields:
        key, kind = f.key, f.kind
        raw = str(form.get(f"value__{key}", "")).strip()
        # A typed-only field (the notice period) counts when a number was typed.
        if form.get(f"accept__{key}") != "1" and (f.prefill or not raw):
            continue
        if kind == "bool":
            values[key] = raw == "true"
        elif raw:
            values[key] = raw
    if not can_see_costs(who):
        values.pop("cost", None)
        values.pop("cost_period", None)
    try:
        async with module.session() as s:
            t, c = await _term_and_contract(s, term_id)
            if values:
                await service.apply_suggestions(s, who, c, t, values, filename=run.filename,
                                                total=len(run.suggestions))  # fmt: skip
            await s.commit()
    except ContractsError as exc:
        return _back(f"/contracts/t/{term_id}/suggestions/{run_id}", str(exc))
    await module.ai.suggestions_reviewed(run_id, values)
    return _back(f"/contracts/c/{c.id}")


# ---- Start from a file: the local model reads it, a person creates the contract -----------


NEW_ENTITY = "new_contract"  # a suggestion run for a contract that doesn't exist yet


@module.router.post("/from-file")
async def start_from_file(
    request: Request, who: CanWrite, file: Uploads = None
) -> RedirectResponse:
    if who.via_ai:
        return _back("/contracts/new", "Starting from a file is done by a person.")
    try:
        stored = await _store_uploads(file)
    except ContractsError as exc:
        return _back("/contracts/new", str(exc))
    if not stored:
        return _back("/contracts/new", "Choose the contract, quote or invoice to read.")
    upload = stored[0]
    try:
        run_id = await module.ai.suggest(
            service.SUGGEST_FEATURE,
            upload["id"],
            service.new_contract_fields(costs=can_see_costs(who)),
            entity=(NEW_ENTITY, str(upload["id"])),
            instructions=service.SUGGEST_INSTRUCTIONS,
        )
    except AIUnavailable as exc:
        return _back("/contracts/new", str(exc))
    return _back(f"/contracts/from-file/{run_id}")


async def _new_run(run_id: int) -> Any:
    run = await module.ai.suggestion_run(run_id)
    if run is None or run.entity_type != NEW_ENTITY:
        raise HTTPException(status_code=404, detail="Nothing to review")
    return run


@module.page(
    "/from-file/{run_id}",
    template="from_file.html",
    title="New contract from a file",
    permission="contracts.write",
)
async def from_file_page(request: Request) -> dict[str, Any]:
    run = await _new_run(int(request.path_params["run_id"]))
    kept = module.forms.recall(request)
    costs = can_see_costs(module.current_principal())
    found = {x.key: x for x in run.suggestions if costs or x.key not in ("cost", "cost_period")}

    def value(key: str, fallback: Any = "") -> Any:
        if kept is not None:
            return kept.get(key, "")
        sg = found.get(key)
        field = next((f for f in run.fields if f.key == key), None)
        if sg is None or (field is not None and not field.prefill):
            return fallback
        if field is not None and field.kind == "bool":
            return bool(sg.value)
        return sg.value

    owners = [("", "Nobody yet")] + await module.people.options(holding="contracts.read")
    pages = await module.files.text(run.file_id) if run.status in ("done", "reviewed") else []
    return {
        "run": run,
        "found": found,
        "file": {"id": run.file_id, "filename": run.filename},
        "pages": pages,
        "categories": list(CATEGORIES.items()),
        "periods": list(COST_PERIODS.items()),
        "owners": owners,
        "can_costs": costs,
        "v": {
            k: value(k)
            for k in (
                "name",
                "vendor",
                "starts_on",
                "ends_on",
                "cost",
                "po_number",
                "seats_bought",
                "seats_in_use",
                "description",
                "owner",
            )
        },
        "category": value("category", "software") or "software",
        "cost_period": value("cost_period", "year") or "year",
        "auto_renews": bool(kept.get("auto_renews"))
        if kept is not None
        else bool(value("auto_renews", False)),
        "notice_days": kept.get("notice_days", "") if kept is not None else "",
        "error": request.query_params.get("error"),
    }


@module.router.post("/from-file/{run_id}")
async def create_from_file(
    request: Request,
    run_id: int,
    who: CanWrite,
    name: Text = "",
    category: Text = "software",
    vendor: Text = "",
    owner: Text = "",
    description: Text = "",
    auto_renews: Text = "",
    notice_days: Text = "0",
    starts_on: Text = "",
    ends_on: Text = "",
    cost: Text = "",
    cost_period: Text = "year",
    po_number: Text = "",
    seats_bought: Text = "",
    seats_in_use: Text = "",
) -> RedirectResponse:
    run = await _new_run(run_id)
    url = f"/contracts/from-file/{run_id}"
    if run.status != "done":
        return _back(url, "This file was already used for a contract, or isn't read yet.")
    info = await module.files.info(run.file_id)
    if info is None:
        return _back(url, "The uploaded file is gone; start again from New contract.")
    data = _contract_input(
        name, category, vendor, owner, description, auto_renews, notice_days or "0", {}
    )
    term = TermInput(starts_on, ends_on, cost, cost_period, po_number, seats_bought, seats_in_use)
    used = {
        "name": data.name,
        "vendor": data.vendor,
        "category": data.category,
        "starts_on": starts_on,
        "ends_on": ends_on,
        "po_number": po_number,
        "seats_bought": seats_bought,
        "auto_renews": data.auto_renews,
        **({"cost": cost, "cost_period": cost_period} if can_see_costs(who) else {}),
    }
    kept_from_file = sorted(
        sg.key for sg in run.suggestions if sg.key in used and str(used[sg.key]) == str(sg.value)
    )
    note = f"; read from {run.filename}" + (
        f" (kept {len(kept_from_file)} suggested value(s))" if kept_from_file else ""
    )
    try:
        async with module.session() as s:
            c = await service.create(
                s,
                who,
                data,
                term,
                note=note,
                files=[
                    {
                        "id": info.id,
                        "filename": info.filename,
                        "size": info.size_bytes,
                        "mime": info.mime_type,
                    }
                ],
            )
            await s.commit()
            cid = c.id
    except ContractsError as exc:
        return await module.forms.back(request, url, str(exc))
    await module.ai.suggestions_reviewed(run_id, {k: used[k] for k in kept_from_file})
    return _back(f"/contracts/c/{cid}")


@module.router.post("/from-file/{run_id}/discard")
async def discard_from_file(run_id: int, who: CanWrite) -> RedirectResponse:
    """The file isn't a contract to add: nothing is created, and it stops waiting."""
    run = await _new_run(run_id)
    if run.status != "done":
        return _back(f"/contracts/from-file/{run_id}", "This file was already dealt with.")
    await module.ai.suggestions_reviewed(run_id, {})
    return _back("/contracts/")
