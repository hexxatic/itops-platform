"""MCP tools for Contracts (exposed as ``contracts_<name>``).

Tools run *via AI* as the connected user, so only people who can see contracts get
anything. They answer what a contract is, when it ends and who owns it, but **never what it
costs** (costs don't go to AI connections, ADR 0014). They can draft a contract and change
drafts; a person accepts it. Renewing, cancelling and archiving are human-only.
"""

from __future__ import annotations

import inspect
from typing import Any

from itops.sdk import Principal
from itops_contracts import service
from itops_contracts.app import module
from itops_contracts.models import Contract, Term
from itops_contracts.service import (
    CATEGORIES,
    STATES,
    ContractInput,
    ContractsError,
    TermInput,
    cancel_by,
    contract_links,
)

DRAFT_HELP = (
    "`category`: one of " + ", ".join(f"'{k}' ({v})" for k, v in CATEGORIES.items()) + ". "
    "`starts_on` / `ends_on`: the current term, as YYYY-MM-DD. `notice_days`: how long before "
    "the end the vendor must be told (0 if none). `owner`: the owner's email (UPN), someone "
    "who can see contracts; leave it out if unsure. Costs aren't taken or shown: a person "
    "adds the price in IT-Ops."
)


def _who() -> Principal:
    p = module.current_principal()
    if p is None:
        raise PermissionError("not signed in")
    return p


def _summary(c: Contract, t: Term | None) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "vendor": c.vendor,
        "category": CATEGORIES.get(c.category, c.category),
        "status": c.status,
        "owner": c.owner_upn,
        "term": {"starts_on": str(t.starts_on), "ends_on": str(t.ends_on), "state": STATES[t.state]}
        if t
        else None,
        "cancel_by": str(cancel_by(c, t)) if t else None,
        "auto_renews": c.auto_renews,
        "url": module.url(f"/c/{c.id}"),
    }


@module.tool(permission="contracts.read")
async def list_contracts(query: str = "", category: str | None = None) -> list[dict[str, Any]]:
    """IT contracts: name, vendor, owner, the current term's dates, its cancel-by date (the
    last day to give notice) and state. Optional `query` (name or vendor) and `category`
    (software, support, cloud, telecom, hardware, other). Costs are never included."""
    if category is not None and category not in CATEGORIES:
        raise ContractsError(f"category must be one of {', '.join(CATEGORIES)}.")
    async with module.session() as s:
        found = await service.contracts(s, category=category, query=query)
        return [_summary(c, await service.current_term(s, c.id)) for c in found]


@module.tool(permission="contracts.read")
async def renewals_due(within_days: int = 120) -> list[dict[str, Any]]:
    """Contracts coming up for renewal: undecided ones whose cancel-by date is within
    `within_days` (or already past: `days_left` negative), soonest first."""
    async with module.session() as s:
        coming = await service.upcoming(s, within_days=max(0, min(within_days, 730)))
    return [{**_summary(u.contract, u.term), "days_left": u.days_left} for u in coming]


@module.tool(permission="contracts.read")
async def get_contract(contract_id: str) -> dict[str, Any]:
    """One contract in full: details, notes, links, and every term (dates, state, PO number,
    seats, who decided). Costs are never included."""
    async with module.session() as s:
        c = await service.get(s, contract_id)
        if c is None:
            raise ContractsError(f"No contract {contract_id!r}. Use list_contracts to find it.")
        history = await service.terms(s, c.id)
    return {
        **_summary(c, history[0] if history else None),
        "description": c.description or "",
        "notice_days": c.notice_days,
        "links": contract_links(c),
        "terms": [
            {
                "starts_on": str(t.starts_on),
                "ends_on": str(t.ends_on),
                "state": STATES[t.state],
                "po_number": t.po_number,
                "seats_bought": t.seats_bought,
                "seats_in_use": t.seats_in_use,
                "decided_by": t.decided_by,
                "decision_note": t.decision_note,
            }
            for t in history
        ],
    }


async def draft_contract(
    name: str,
    category: str,
    starts_on: str,
    ends_on: str,
    vendor: str = "",
    notice_days: int = 0,
    auto_renews: bool = False,
    owner: str | None = None,
    description: str = "",
    po_number: str = "",
    seats_bought: int | None = None,
) -> dict[str, Any]:
    """Draft a new contract with its current term. It stays a DRAFT until a person accepts it
    in IT-Ops; tell the user the returned `url` to review it."""
    data = ContractInput(
        name=name,
        category=category,
        vendor=vendor,
        owner_upn=owner,
        description=description,
        auto_renews=auto_renews,
        notice_days=notice_days,
    )
    term = TermInput(starts_on, ends_on, po_number=po_number, seats_bought=seats_bought)
    async with module.session() as s:
        c = await service.create(s, _who(), data, term)
        await s.commit()
        result = _summary(c, await service.current_term(s, c.id))
    return {**result, "next": f"Draft saved. A person reviews and accepts it: {result['url']}"}


async def update_draft(
    contract_id: str,
    name: str | None = None,
    category: str | None = None,
    vendor: str | None = None,
    notice_days: int | None = None,
    auto_renews: bool | None = None,
    owner: str | None = None,
    description: str | None = None,
    starts_on: str | None = None,
    ends_on: str | None = None,
    po_number: str | None = None,
    seats_bought: int | None = None,
) -> dict[str, Any]:
    """Change a DRAFT contract (accepted ones are changed by a person). Leave out what
    stays the same."""
    async with module.session() as s:
        c = await service.get(s, contract_id)
        if c is None:
            raise ContractsError(f"No contract {contract_id!r}.")
        if c.status != "draft":
            raise ContractsError("This contract was accepted; a person changes it in IT-Ops.")
        t = await service.current_term(s, c.id)
        assert t is not None
        data = ContractInput(
            name=name if name is not None else c.name,
            category=category if category is not None else c.category,
            vendor=vendor if vendor is not None else c.vendor,
            owner_upn=owner if owner is not None else c.owner_upn,
            description=description if description is not None else (c.description or ""),
            auto_renews=auto_renews if auto_renews is not None else c.auto_renews,
            notice_days=notice_days if notice_days is not None else c.notice_days,
            links=contract_links(c),
        )
        await service.update(s, _who(), c, data)
        term = TermInput(
            starts_on if starts_on is not None else t.starts_on,
            ends_on if ends_on is not None else t.ends_on,
            po_number=po_number if po_number is not None else t.po_number,
            seats_bought=seats_bought if seats_bought is not None else t.seats_bought,
            seats_in_use=t.seats_in_use,
        )
        await service.update_term(s, _who(), c, t, term)
        await s.commit()
        result = _summary(c, t)
    return {**result, "next": f"Draft saved. A person reviews and accepts it: {result['url']}"}


# Registered here so their descriptions carry the drafting guidance.
for _fn in (draft_contract, update_draft):
    module.tool(
        permission="contracts.write",
        writes=True,
        description="\n\n".join([inspect.getdoc(_fn) or "", DRAFT_HELP]),
    )(_fn)
