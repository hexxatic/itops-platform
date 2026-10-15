"""Contracts service: the one place contracts change (ADR 0014).

Pages, AI tools and the daily renewals job all call these functions, so the rules live once:

* A **contract** holds what stays the same (vendor, owner, category, renewal terms); each
  **term** is a period with its own dates, cost, PO number, seats and files. The newest
  term is the current one; renewing adds the next.
* **Cancel-by date** = the current term's end minus the notice period. Reminders and lists
  work from it, never from the end date alone.
* A term moves *active* -> *due* (its notice window opened, 120 days before cancel-by) ->
  *negotiating* -> *renewed* / *cancelled* (a person with ``contracts.approve`` decides) or
  *lapsed* (ended undecided, not auto-renewing).
* **People edit directly**; every change is audited. **AI drafts**: an AI connection may
  create a draft contract and change drafts; a person accepts it. Services refuse AI on
  everything else (tokens reach the REST API too).
* **Costs** are shown and changed only with ``contracts.costs``, and never given to an AI
  connection.

Callers pass a session and commit.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal, SuggestField
from itops.sdk.content import Passage
from itops.sdk.types import FieldDef, TypeDef, TypeProblem, validate_values
from itops_contracts.app import module
from itops_contracts.models import Contract, Term, utcnow

CATEGORIES = {
    "software": "Software and licenses",
    "support": "Support and maintenance",
    "cloud": "Cloud services",
    "telecom": "Telecom and internet",
    "hardware": "Hardware lease",
    "other": "Other",
}
COST_PERIODS = {"month": "per month", "year": "per year", "once": "one-off"}
STATES = {
    "active": "Active",
    "due": "Renewal due",
    "negotiating": "Negotiating",
    "renewed": "Renewed",
    "cancelled": "Cancelled",
    "lapsed": "Lapsed",
}
OPEN_STATES = ("active", "due", "negotiating")  # not decided yet
REMINDERS = (120, 90, 60, 30)  # days before cancel-by (Justin, 2026-10-13)
WINDOW_DAYS = REMINDERS[0]
OVERDUE_EVERY_DAYS = 7

# The contract's links, as a record-type definition so core's pickers and labels work.
LINKS = TypeDef(
    name="Contract links",
    fields=[
        FieldDef(
            key="vendor",
            label="Vendor document",
            kind="link",
            target="docs",
            help="The vendor's page in Docs (contacts, portal, support).",
        ),
        FieldDef(
            key="covers",
            label="Covers",
            kind="link",
            target="assets",
            multiple=True,
            help="The assets this contract covers (software, devices, circuits).",
        ),
    ],
)


class ContractsError(ValueError):
    """A user-facing problem (shown as a message, not a crash)."""


def today() -> dt.date:
    return dt.date.today()


# ---- rules -----------------------------------------------------------------------------


def _no_ai(who: Principal, what: str = "change contracts") -> None:
    if who.via_ai:
        raise ContractsError(
            f"An AI connection can't {what}: it drafts a contract "
            "(contracts_draft_contract) and a person accepts it in IT-Ops."
        )


def can_see_costs(who: Principal | None) -> bool:
    return bool(who and not who.via_ai and who.has("contracts.costs"))


def contract_links(c: Contract) -> dict[str, Any]:
    if not c.links:
        return {}
    values: dict[str, Any] = json.loads(c.links)
    return values


def term_attachments(t: Term) -> list[dict[str, Any]]:
    if not t.attachments:
        return []
    items: list[dict[str, Any]] = json.loads(t.attachments)
    return items


def cancel_by(c: Contract, t: Term) -> dt.date:
    """The last day to give notice: the term's end minus the notice period."""
    return t.ends_on - dt.timedelta(days=c.notice_days)


def yearly(t: Term) -> Decimal | None:
    """A recurring term's cost per year (one-off costs aren't yearly)."""
    if t.cost is None or t.cost_period == "once":
        return None
    return t.cost * 12 if t.cost_period == "month" else t.cost


def _clean_text(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def _parse_date(value: str | dt.date | None, label: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError as exc:
        raise ContractsError(f"{label}: use a date like 2027-03-31.") from exc


def _parse_int(
    value: str | int | None, label: str, *, low: int = 0, high: int = 1_000_000
) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(str(value).replace(",", "").strip())
    except ValueError as exc:
        raise ContractsError(f"{label}: a whole number.") from exc
    if not low <= n <= high:
        raise ContractsError(f"{label}: between {low} and {high}.")
    return n


def _parse_cost(value: str | Decimal | float | None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        cost = Decimal(str(value).replace(",", "").replace("$", "").strip()).quantize(
            Decimal("0.01")
        )
    except InvalidOperation as exc:
        raise ContractsError("Cost: a number, like 1250.00.") from exc
    if cost < 0 or cost >= Decimal("10000000000"):
        raise ContractsError("Cost: between 0 and 9,999,999,999.99.")
    return cost


@dataclass
class ContractInput:
    """What a person (or an AI, for a draft) gives for a contract."""

    name: str
    category: str
    vendor: str = ""
    owner_upn: str | None = None
    description: str = ""
    auto_renews: bool = False
    notice_days: int | str | None = 0
    links: dict[str, Any] | None = None


@dataclass
class TermInput:
    starts_on: str | dt.date | None
    ends_on: str | dt.date | None
    cost: str | Decimal | float | None = None
    cost_period: str = "year"
    po_number: str = ""
    seats_bought: int | str | None = None
    seats_in_use: int | str | None = None


async def _owner(upn: str | None) -> str | None:
    """The owner must be someone who can see contracts (or nobody yet)."""
    upn = (upn or "").strip()
    if not upn:
        return None
    allowed = {u.lower(): u for u, _ in await module.people.options(holding="contracts.read")}
    if upn.lower() not in allowed:
        raise ContractsError("The owner must be someone who can see contracts in IT-Ops.")
    return allowed[upn.lower()]


async def _apply_contract(c: Contract, data: ContractInput) -> None:
    name = _clean_text(data.name, 300)
    if not name:
        raise ContractsError("Give the contract a name.")
    if data.category not in CATEGORIES:
        raise ContractsError(f"Category: choose one of {', '.join(CATEGORIES.values())}.")
    notice = _parse_int(data.notice_days, "Notice period (days)", high=730) or 0
    try:
        links = validate_values(LINKS, data.links or {}, require=False)
    except TypeProblem as exc:
        raise ContractsError(str(exc)) from exc
    c.name = name
    c.category = data.category
    c.vendor = _clean_text(data.vendor, 200)
    c.owner_upn = await _owner(data.owner_upn)
    c.description = (data.description or "").strip()[:20000] or None
    c.auto_renews = bool(data.auto_renews)
    c.notice_days = notice
    c.links = json.dumps(links) if links else None


def _apply_term(t: Term, data: TermInput, *, costs: bool) -> None:
    starts = _parse_date(data.starts_on, "Starts")
    ends = _parse_date(data.ends_on, "Ends")
    if ends < starts:
        raise ContractsError("The term ends before it starts.")
    if data.cost_period not in COST_PERIODS:
        raise ContractsError("Billing: per month, per year or one-off.")
    t.starts_on, t.ends_on = starts, ends
    t.po_number = _clean_text(data.po_number, 100)
    t.seats_bought = _parse_int(data.seats_bought, "Seats bought")
    t.seats_in_use = _parse_int(data.seats_in_use, "Seats in use")
    if costs:  # without contracts.costs the cost stays as it was
        t.cost = _parse_cost(data.cost)
        t.cost_period = data.cost_period


async def _audit(s: AsyncSession, c: Contract, verb: str, summary: str, **details: Any) -> None:
    await module.audit.record(
        s,
        f"contract.{verb}",
        kind="contract",
        entity_id=str(c.id),
        title=c.name,
        summary=summary,
        details=details or None,
    )


# ---- reads -----------------------------------------------------------------------------


def _parse_id(value: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def get(s: AsyncSession, contract_id: str | uuid.UUID) -> Contract | None:
    cid = _parse_id(contract_id)
    return await s.get(Contract, cid) if cid else None


async def get_term(s: AsyncSession, term_id: str | uuid.UUID) -> Term | None:
    tid = _parse_id(term_id)
    return await s.get(Term, tid) if tid else None


async def terms(s: AsyncSession, contract_id: uuid.UUID) -> list[Term]:
    """Newest first: the first is the current term."""
    rows = await s.execute(
        select(Term).where(Term.contract_id == contract_id).order_by(Term.starts_on.desc())
    )
    return list(rows.scalars())


async def current_term(s: AsyncSession, contract_id: uuid.UUID) -> Term | None:
    found = await terms(s, contract_id)
    return found[0] if found else None


async def contracts(
    s: AsyncSession,
    *,
    status: str | None = "active",
    category: str | None = None,
    owner: str | None = None,
    query: str = "",
) -> list[Contract]:
    q = select(Contract).order_by(Contract.name)
    if status:
        q = q.where(Contract.status == status)
    if category:
        q = q.where(Contract.category == category)
    if owner:
        q = q.where(Contract.owner_upn == owner)
    if query.strip():
        like = f"%{query.strip()}%"
        q = q.where(or_(Contract.name.ilike(like), Contract.vendor.ilike(like)))
    return list((await s.execute(q)).scalars())


@dataclass
class Upcoming:
    """A contract whose current term is undecided, with its renewal dates."""

    contract: Contract
    term: Term
    cancel_by: dt.date
    days_left: int  # negative: overdue


async def upcoming(s: AsyncSession, *, within_days: int = WINDOW_DAYS) -> list[Upcoming]:
    """Active contracts whose current term is undecided and whose cancel-by date is within
    ``within_days`` (or already past), soonest first."""
    now = today()
    out: list[Upcoming] = []
    for c in await contracts(s):
        t = await current_term(s, c.id)
        if t is None or t.state not in OPEN_STATES:
            continue
        by = cancel_by(c, t)
        left = (by - now).days
        if left <= within_days:
            out.append(Upcoming(c, t, by, left))
    return sorted(out, key=lambda u: u.cancel_by)


# ---- writes ----------------------------------------------------------------------------


async def create(
    s: AsyncSession,
    who: Principal,
    data: ContractInput,
    term: TermInput,
    *,
    files: list[dict[str, Any]] | None = None,
    note: str = "",
) -> Contract:
    """A contract with its first term (and its files: uploads, claimed here). A person's is
    active at once; an AI connection's is a draft for a person to accept (and never carries
    a cost). ``note`` adds to the audit entry (e.g. which file it was read from)."""
    c = Contract(
        status="draft" if who.via_ai else "active",
        created_by=who.user,
        created_via_ai=who.via_ai,
        updated_by=who.user,
    )
    await _apply_contract(c, data)
    t = Term(contract_id=c.id, state="active", created_by=who.user)
    _apply_term(t, term, costs=can_see_costs(who))
    s.add(c)
    await s.flush()
    t.contract_id = c.id
    if files:
        t.attachments = json.dumps(files)
    s.add(t)
    await s.flush()
    if files:
        await module.files.claim(
            s, [str(a["id"]) for a in files], entity_type="term", entity_id=str(t.id)
        )
    if c.status == "draft":
        await _audit(s, c, "drafted", "Drafted by AI, waiting for a person to accept it")
    else:
        await _audit(s, c, "created", f"Created; current term {t.starts_on} to {t.ends_on}{note}")
        await _index(s, c)
    return c


async def update(s: AsyncSession, who: Principal, c: Contract, data: ContractInput) -> None:
    """Change a contract's details. AI connections may only change drafts."""
    if who.via_ai and c.status != "draft":
        _no_ai(who, "change an accepted contract")
    if c.status == "archived":
        raise ContractsError("This contract is archived. Restore it before changing it.")
    before = {
        "name": c.name,
        "vendor": c.vendor,
        "category": c.category,
        "owner": c.owner_upn,
        "auto_renews": c.auto_renews,
        "notice_days": c.notice_days,
    }
    await _apply_contract(c, data)
    after = {
        "name": c.name,
        "vendor": c.vendor,
        "category": c.category,
        "owner": c.owner_upn,
        "auto_renews": c.auto_renews,
        "notice_days": c.notice_days,
    }
    changed = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
    c.updated_by, c.updated_at = who.user, utcnow()
    await _audit(
        s, c, "changed", "Changed " + (", ".join(changed) or "the description or links"), **changed
    )
    await _index(s, c)


async def update_term(
    s: AsyncSession,
    who: Principal,
    c: Contract,
    t: Term,
    data: TermInput,
    *,
    add_files: list[dict[str, Any]] | None = None,
    remove_files: set[str] | None = None,
) -> None:
    """Change a term's dates, cost, PO, seats and files."""
    if who.via_ai and c.status != "draft":
        _no_ai(who, "change an accepted contract's terms")
    if c.status == "archived":
        raise ContractsError("This contract is archived. Restore it before changing it.")
    old = (t.starts_on, t.ends_on)
    _apply_term(t, data, costs=can_see_costs(who))
    files = [a for a in term_attachments(t) if str(a["id"]) not in (remove_files or set())]
    files += add_files or []
    t.attachments = json.dumps(files) if files else None
    await module.files.claim(
        s, [str(a["id"]) for a in files], entity_type="term", entity_id=str(t.id)
    )
    if (t.starts_on, t.ends_on) != old and t.state in OPEN_STATES:
        t.notified, t.overdue_notified_on = "", None  # new dates: reminders start over
        if t.state == "due" and (cancel_by(c, t) - today()).days > WINDOW_DAYS:
            t.state = "active"
    c.updated_by, c.updated_at = who.user, utcnow()
    await _audit(s, c, "term_changed", f"Changed the term {t.starts_on} to {t.ends_on}")


async def accept(s: AsyncSession, who: Principal, c: Contract) -> None:
    """A person accepts an AI-drafted contract: it becomes active."""
    _no_ai(who, "accept a drafted contract")
    if c.status != "draft":
        raise ContractsError("Only a draft can be accepted.")
    c.status, c.updated_by, c.updated_at = "active", who.user, utcnow()
    await _audit(s, c, "accepted", "Accepted the AI-drafted contract")
    await _index(s, c)


async def discard_draft(s: AsyncSession, who: Principal, c: Contract) -> None:
    _no_ai(who, "discard a drafted contract")
    if c.status != "draft":
        raise ContractsError("Only a draft can be discarded; archive an accepted contract.")
    await _audit(s, c, "discarded", "Discarded the AI-drafted contract")
    for t in await terms(s, c.id):
        await s.delete(t)
    await s.delete(c)


async def start_negotiating(s: AsyncSession, who: Principal, c: Contract) -> None:
    _no_ai(who)
    t = await current_term(s, c.id)
    if t is None or t.state not in ("active", "due"):
        raise ContractsError("Only an undecided term can go into negotiation.")
    t.state = "negotiating"
    await _audit(
        s, c, "negotiating", f"Started negotiating the renewal (cancel by {cancel_by(c, t)})"
    )


async def renew(
    s: AsyncSession, who: Principal, c: Contract, term: TermInput, *, note: str = ""
) -> Term:
    """Decide to renew: the current term is marked renewed and the next one begins."""
    _no_ai(who, "renew a contract")
    current = await current_term(s, c.id)
    if c.status != "active" or current is None or current.state not in (*OPEN_STATES, "lapsed"):
        raise ContractsError("Only an active contract with an undecided term can be renewed.")
    nxt = Term(contract_id=c.id, state="active", created_by=who.user)
    if not can_see_costs(who):  # carry the cost over for someone who can't see it
        nxt.cost, nxt.cost_period = current.cost, current.cost_period
    _apply_term(nxt, term, costs=can_see_costs(who))
    if nxt.starts_on <= current.starts_on:
        raise ContractsError("The new term must start after the current one.")
    current.state, current.decided_by, current.decided_at = "renewed", who.user, utcnow()
    current.decision_note = _clean_text(note, 500)
    s.add(nxt)
    c.updated_by, c.updated_at = who.user, utcnow()
    await s.flush()
    await _audit(
        s,
        c,
        "renewed",
        f"Renewed: new term {nxt.starts_on} to {nxt.ends_on}"
        + (f" ({current.decision_note})" if current.decision_note else ""),
    )
    module.publish(
        "renewed", {"id": str(c.id), "name": c.name, "term": f"{nxt.starts_on}/{nxt.ends_on}"}
    )
    return nxt


async def cancel(s: AsyncSession, who: Principal, c: Contract, *, note: str = "") -> None:
    """Decide not to renew: the current term runs to its end and the contract is done."""
    _no_ai(who, "cancel a contract")
    t = await current_term(s, c.id)
    if c.status != "active" or t is None or t.state not in OPEN_STATES:
        raise ContractsError("Only an active contract with an undecided term can be cancelled.")
    t.state, t.decided_by, t.decided_at = "cancelled", who.user, utcnow()
    t.decision_note = _clean_text(note, 500)
    await _audit(
        s,
        c,
        "cancelled",
        f"Cancelled: ends {t.ends_on}" + (f" ({t.decision_note})" if t.decision_note else ""),
    )
    module.publish("cancelled", {"id": str(c.id), "name": c.name, "ends_on": str(t.ends_on)})


async def archive(s: AsyncSession, who: Principal, c: Contract) -> None:
    _no_ai(who, "archive a contract")
    if c.status != "active":
        raise ContractsError("Only an active contract can be archived.")
    c.status, c.updated_by, c.updated_at = "archived", who.user, utcnow()
    await _audit(s, c, "archived", "Archived")
    await _index(s, c)


async def restore(s: AsyncSession, who: Principal, c: Contract) -> None:
    _no_ai(who, "restore a contract")
    if c.status != "archived":
        raise ContractsError("Only an archived contract can be restored.")
    c.status, c.updated_by, c.updated_at = "active", who.user, utcnow()
    await _audit(s, c, "restored", "Restored")
    await _index(s, c)


# ---- AI suggestions from a term's files (ADR 0015) ---------------------------------------

SUGGEST_FEATURE = "suggest"
SUGGEST_INSTRUCTIONS = (
    "The document is an IT contract, quote, invoice, license schedule or vendor email about "
    "one subscription or service. Only report values for the subscription itself."
)


def suggest_fields(*, costs: bool) -> list[SuggestField]:
    """What a term's file can tell us. Costs only for people who may see them; the notice
    period is never pre-filled (models take other day counts for it)."""
    out = [
        SuggestField(
            "starts_on",
            "Term starts",
            "date",
            help="the start date of the current subscription or service term",
        ),
        SuggestField(
            "ends_on",
            "Term ends",
            "date",
            help="the end, expiry or renewal date of the current term",
        ),
        SuggestField(
            "seats_bought",
            "Seats bought",
            "number",
            help="number of licenses, users or seats purchased",
        ),
        SuggestField("po_number", "PO number", "text", help="purchase order number"),
        SuggestField(
            "auto_renews",
            "Renews automatically",
            "bool",
            help="true only if the text says it renews automatically unless cancelled",
        ),
        SuggestField(
            "notice_days",
            "Notice period (days)",
            "number",
            prefill=False,
            help="days before the end that written notice of cancellation is required",
        ),
    ]
    if costs:
        out[2:2] = [
            SuggestField("cost", "Cost", "number", help="the total price of the term"),
            SuggestField(
                "cost_period",
                "Billing",
                "select",
                options=("month", "year", "once"),
                help="month if billed monthly, year if yearly, once if one payment",
            ),
        ]
    return out


def new_contract_fields(*, costs: bool) -> list[SuggestField]:
    """What a file can tell us about a contract that doesn't exist yet ("Start from a
    file"): what it is and who sells it, then everything a term's file gives."""
    return [
        SuggestField(
            "name", "Name", "text",
            help="the product, service or subscription the document is about, as written "
            "(e.g. 'Microsoft 365 E3', 'Managed SD-WAN')",
        ),
        SuggestField("vendor", "Vendor", "text", help="the company selling or providing it"),
        SuggestField(
            "category", "Category", "select", options=tuple(CATEGORIES),
            help="software, support, cloud, telecom, hardware, or other",
        ),
        *suggest_fields(costs=costs),
    ]  # fmt: skip


async def apply_suggestions(
    s: AsyncSession,
    who: Principal,
    c: Contract,
    t: Term,
    values: dict[str, Any],
    *,
    filename: str,
    total: int,
) -> list[str]:
    """Save the values a person accepted from a file's suggestions; return what changed."""
    _no_ai(who, "accept suggestions")
    term = TermInput(
        starts_on=values.get("starts_on", t.starts_on),
        ends_on=values.get("ends_on", t.ends_on),
        cost=values.get("cost", t.cost),
        cost_period=values.get("cost_period", t.cost_period),
        po_number=values.get("po_number", t.po_number),
        seats_bought=values.get("seats_bought", t.seats_bought),
        seats_in_use=t.seats_in_use,
    )
    before_term = (t.starts_on, t.ends_on, t.cost, t.cost_period, t.po_number, t.seats_bought)
    await update_term(s, who, c, t, term)
    changed = [
        k
        for k, (a, b) in zip(
            ("starts_on", "ends_on", "cost", "cost_period", "po_number", "seats_bought"),
            zip(
                before_term,
                (t.starts_on, t.ends_on, t.cost, t.cost_period, t.po_number, t.seats_bought),
                strict=True,
            ),
            strict=True,
        )
        if a != b
    ]
    if "notice_days" in values or "auto_renews" in values:
        data = ContractInput(
            name=c.name,
            category=c.category,
            vendor=c.vendor,
            owner_upn=c.owner_upn,
            description=c.description or "",
            links=contract_links(c),
            auto_renews=values.get("auto_renews", c.auto_renews),
            notice_days=values.get("notice_days", c.notice_days),
        )
        before = (c.notice_days, c.auto_renews)
        await update(s, who, c, data)
        changed += [
            k
            for k, a, b in (
                ("notice_days", before[0], c.notice_days),
                ("auto_renews", before[1], c.auto_renews),
            )
            if a != b
        ]
    await _audit(
        s,
        c,
        "suggestions_accepted",
        f"Accepted {len(values)} of {total} suggestion(s) from {filename} (local AI)",
        accepted=sorted(values),
        changed=changed,
    )
    return changed


# ---- the daily job ------------------------------------------------------------------------


async def check_renewals(s: AsyncSession, on: dt.date | None = None) -> dict[str, int]:
    """What the daily job does (``on``: the day to act as, for tests). Returns counts."""
    now = on or today()
    counts = {"due": 0, "reminded": 0, "overdue": 0, "lapsed": 0}
    for c in await contracts(s):
        t = await current_term(s, c.id)
        if t is None or t.state not in OPEN_STATES:
            continue
        by = cancel_by(c, t)
        left = (by - now).days
        if t.state == "active" and left <= WINDOW_DAYS:
            t.state = "due"
            counts["due"] += 1
            await _audit(s, c, "renewal_due", f"Renewal due: cancel by {by} ({left} days)")
        sent = {int(d) for d in t.notified.split(",") if d}
        crossed = [d for d in REMINDERS if left <= d and d not in sent]
        if crossed and left >= 0:
            t.notified = ",".join(str(d) for d in sorted(sent | set(crossed), reverse=True))
            counts["reminded"] += 1
            module.publish(
                "renewal_due",
                {
                    "id": str(c.id),
                    "name": c.name,
                    "owner": c.owner_upn,
                    "cancel_by": str(by),
                    "days_left": left,
                    "reminder": min(crossed),
                },
            )
        if now > t.ends_on and not c.auto_renews:
            t.state, t.decided_at = "lapsed", utcnow()
            counts["lapsed"] += 1
            await _audit(s, c, "lapsed", f"Lapsed: ended {t.ends_on} without a decision")
            continue
        if left < 0 and (
            t.overdue_notified_on is None
            or (now - t.overdue_notified_on).days >= OVERDUE_EVERY_DAYS
        ):
            t.overdue_notified_on = now
            counts["overdue"] += 1
            module.publish(
                "renewal_overdue",
                {
                    "id": str(c.id),
                    "name": c.name,
                    "owner": c.owner_upn,
                    "cancel_by": str(by),
                    "days_overdue": -left,
                },
            )
    return counts


@module.job("renewals")
async def renewals_job() -> str:
    async with module.session() as s:
        counts = await check_renewals(s)
        await s.commit()
    parts = [f"{n} {label}" for label, n in counts.items() if n]
    return ", ".join(parts) if parts else "Nothing came due"


# ---- search --------------------------------------------------------------------------------


async def _index(s: AsyncSession, c: Contract) -> None:
    """Accepted contracts are searchable (never their costs); drafts aren't."""
    if c.status == "draft":
        await module.search.remove(s, kind="contract", entity_id=str(c.id))
        return
    lines = [
        c.name,
        f"Vendor: {c.vendor}" if c.vendor else "",
        f"Category: {CATEGORIES.get(c.category, c.category)}",
        c.description or "",
    ]
    await module.search.index(
        s,
        kind="contract",
        entity_id=str(c.id),
        title=c.name,
        path=f"/c/{c.id}",
        passages=[Passage(label="Contract", anchor=None, text="\n".join(x for x in lines if x))],
        read_permission="contracts.read",
        archived=c.status == "archived",
    )


@module.search.reindexer
async def reindex_all() -> int:
    count = 0
    async with module.session() as s:
        for c in list((await s.execute(select(Contract))).scalars()):
            await _index(s, c)
            await s.commit()  # one at a time: never one long transaction
            count += 1
        await s.commit()
    return count


# ---- services other modules use --------------------------------------------------------


@module.service("lookup")
async def lookup(
    *, ids: list[str] | None = None, kind: str | None = None, query: str = "", limit: int = 50
) -> list[dict[str, str]]:
    """Contracts for link pickers and labels: by ``ids``, or by name ``query``. Each: {id,
    kind "contract", name, url}. Someone who can't see contracts gets them as restricted."""
    if kind not in (None, "contract"):
        return []
    who = module.current_principal()
    visible = bool(who and who.has("contracts.read"))
    async with module.session() as s:
        if ids is not None:
            found = [c for i in ids if (c := await get(s, i)) is not None]
        else:
            found = (await contracts(s, query=query))[: max(1, min(limit, 500))] if visible else []
    return [
        {"id": str(c.id), "kind": "contract", "name": c.name, "url": f"/contracts/c/{c.id}"}
        if visible
        else {"id": str(c.id), "kind": "contract", "restricted": "1"}
        for c in found
    ]


def _link_text(item: Any) -> str:
    """A stored link as "module:kind:id"."""
    if isinstance(item, dict):
        d: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
        return ":".join(str(d.get(k, "")) for k in ("module", "kind", "id"))
    return str(item)


@module.service("related")
async def related(link: str, limit: int = 50) -> list[dict[str, str]]:
    """Active contracts that link to ``link`` (a vendor document, a covered asset). Each:
    {id, kind, name, url, field}. Nothing for people who can't see contracts."""
    who = module.current_principal()
    if not (who and who.has("contracts.read")):
        return []
    out: list[dict[str, str]] = []
    labels = {f.key: f.label for f in LINKS.fields}
    async with module.session() as s:
        for c in await contracts(s):
            for key, value in contract_links(c).items():
                items: list[Any] = value if isinstance(value, list) else [value]  # pyright: ignore[reportUnknownVariableType]
                if link in {_link_text(i) for i in items}:
                    out.append(
                        {
                            "id": str(c.id),
                            "kind": "contract",
                            "name": c.name,
                            "url": f"/contracts/c/{c.id}",
                            "field": labels.get(key, key),
                        }
                    )
                    break
    return out[: max(1, min(limit, 200))]


@module.service("upsert")
async def upsert(
    *,
    source_type: str,
    source_id: str,
    contract: dict[str, Any],
    term: dict[str, Any],
    files: list[str] | None = None,
) -> dict[str, Any]:
    """Create the contract a source record maps to (connector runs only, ADR 0010), e.g.
    moving Docs' Contract documents in. ``contract``: ContractInput's fields; ``term``:
    TermInput's; ``files``: file ids (any module's, the connector must be able to read them)
    copied onto the term. Imported once: a later run never overwrites what people changed
    since; it reports the contract unchanged or kept. In a dry run nothing is written.
    Returns {outcome: created|unchanged|kept|failed, id, problem}."""
    run = module.refs.run
    if run is None:
        raise ContractsError("contracts.upsert is for connector runs (ADR 0010).")
    who = module.current_principal()
    if who is None or not who.has("contracts.write"):
        return {
            "outcome": "failed",
            "problem": "The connector needs the contracts.write permission.",
        }
    fp = module.refs.fingerprint({"contract": contract, "term": term, "files": files or []})
    try:
        data, term_data = ContractInput(**contract), TermInput(**term)
        async with module.session() as s:
            ref = await module.refs.find(s, source_type, source_id)
            existing = await get(s, ref.entity_id) if ref else None
            if ref is not None and existing is not None:
                if not run.dry_run:
                    await module.refs.seen(s, ref, fingerprint=ref.fingerprint)
                    await s.commit()
                outcome = "unchanged" if ref.fingerprint == fp else "kept"
                return {"outcome": outcome, "id": str(existing.id)}
            if run.dry_run:  # check it would save, write nothing
                await _apply_contract(Contract(), data)
                _apply_term(Term(), term_data, costs=True)
                return {"outcome": "created", "id": None}
            c = await create(s, who, data, term_data)
            t = await current_term(s, c.id)
            assert t is not None
            copies = [await module.files.copy(fid) for fid in files or []]
            if copies:
                t.attachments = json.dumps(
                    [
                        {
                            "id": f.id,
                            "filename": f.filename,
                            "size": f.size_bytes,
                            "mime": f.mime_type,
                        }
                        for f in copies
                    ]
                )
                await module.files.claim(
                    s, [f.id for f in copies], entity_type="term", entity_id=str(t.id)
                )
            await module.refs.link(
                s, source_type, source_id, kind="contract", entity_id=str(c.id), fingerprint=fp
            )
            await s.commit()
            return {"outcome": "created", "id": str(c.id)}
    except (ContractsError, TypeError) as exc:
        return {"outcome": "failed", "problem": str(exc)}


@module.service("link_targets")
async def link_targets() -> list[dict[str, str]]:
    return [{"target": "contracts", "label": "Contracts: a contract"}]
