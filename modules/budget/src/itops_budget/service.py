"""Budget service: the one place Budget's inputs change, and where the forecast's inputs
are gathered (ADR 0016).

* **Inputs from other modules** come only through their services: the fleet from
  ``assets.fleet``, HR and purchase history from ``reports.rows``. Both respect the viewer
  (restricted asset types and datasets, money columns only with ``reports.costs``).
* **Budget's own inputs** are the assumptions (one JSON setting) and the price list. People
  change them directly; every change is audited. AI connections never change them: they
  propose changes (checked like a real save) that a person approves or rejects
  (``budget.approve``, human-only).
* **Saved versions** freeze the assumptions, prices and the forecast as JSON. Marking one
  adopted is ``budget.approve`` (human-only); only one version is adopted at a time.

Callers pass a session and commit.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal
from itops_budget import engine
from itops_budget.app import module
from itops_budget.engine import PRICE_ALIASES, PRICE_CATEGORIES, Assumptions, Forecast
from itops_budget.models import Exclusion, Price, Proposal, Setting, Skip, Version, utcnow

ASSUMPTIONS = "assumptions"


class BudgetError(ValueError):
    """A user-facing problem (shown as a message, not a crash)."""


def _no_ai(who: Principal) -> None:
    # Every figure is money and AI never changes figures (ADR 0016); a token reaches REST too.
    if who.via_ai:
        raise BudgetError("An AI connection can't change the budget: a person does.")


# ---- assumptions ---------------------------------------------------------------------------


async def assumptions(s: AsyncSession) -> Assumptions:
    row = await s.get(Setting, ASSUMPTIONS)
    if row is None:
        return Assumptions()
    try:
        return Assumptions.model_validate_json(row.value)
    except ValidationError:
        return Assumptions()  # a setting saved by an older version: start from the defaults


async def save_assumptions(s: AsyncSession, who: Principal, new: Assumptions) -> list[str]:
    """Save the assumptions; returns the labels of what changed (audited with before/after)."""
    _no_ai(who)
    old = await assumptions(s)
    before, after = old.model_dump(), new.model_dump()
    changed = [k for k in after if before.get(k) != after[k]]
    if not changed:
        return []
    row = await s.get(Setting, ASSUMPTIONS)
    if row is None:
        s.add(Setting(key=ASSUMPTIONS, value=new.model_dump_json(), updated_by=who.user))
    else:
        row.value, row.updated_by, row.updated_at = new.model_dump_json(), who.user, utcnow()
    await module.audit.record(
        s,
        "assumptions.changed",
        kind="assumptions",
        entity_id=ASSUMPTIONS,
        title="Budget assumptions",
        summary="Changed " + ", ".join(changed),
        details={k: {"from": before.get(k), "to": after[k]} for k in changed},
    )
    return changed


# ---- the price list ------------------------------------------------------------------------


async def prices(s: AsyncSession) -> list[Price]:
    q = select(Price).order_by(Price.category, Price.item)
    return list((await s.execute(q)).scalars())


def _price_values(
    item: str, category: str, unit_price: str, source: str, quoted_on: str
) -> tuple[str, str, Decimal, str, dt.date | None]:
    item = " ".join(item.split())[:200]
    if not item:
        raise BudgetError("Give the item a name, e.g. “Standard laptop”.")
    category = PRICE_ALIASES.get(category, category)  # laptop = the standard laptop
    if category not in PRICE_CATEGORIES:
        raise BudgetError("Choose what the price is for.")
    try:
        price = Decimal(unit_price.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        raise BudgetError(f"“{unit_price}” isn't a price.") from None
    if price < 0 or price >= Decimal("10000000"):
        raise BudgetError("The price has to be between 0 and 9,999,999.")
    try:
        quoted = dt.date.fromisoformat(quoted_on) if quoted_on.strip() else None
    except ValueError:
        raise BudgetError(f"“{quoted_on}” isn't a date (YYYY-MM-DD).") from None
    return item, category, price.quantize(Decimal("0.01")), " ".join(source.split())[:300], quoted


async def add_price(
    s: AsyncSession,
    who: Principal,
    *,
    item: str,
    category: str,
    unit_price: str,
    source: str = "",
    quoted_on: str = "",
) -> Price:
    _no_ai(who)
    item, category, price, source, quoted = _price_values(
        item, category, unit_price, source, quoted_on
    )
    p = Price(
        item=item,
        category=category,
        unit_price=price,
        source=source,
        quoted_on=quoted,
        updated_by=who.user,
    )
    s.add(p)
    await s.flush()
    await module.audit.record(
        s,
        "price.added",
        kind="price",
        entity_id=str(p.id),
        title=item,
        summary=f"Added {item}: ${price:,.2f} ({PRICE_CATEGORIES[category]})",
    )
    return p


async def update_price(
    s: AsyncSession,
    who: Principal,
    p: Price,
    *,
    item: str,
    category: str,
    unit_price: str,
    source: str = "",
    quoted_on: str = "",
) -> None:
    _no_ai(who)
    item, category, price, source, quoted = _price_values(
        item, category, unit_price, source, quoted_on
    )
    before = (p.item, p.category, p.unit_price, p.source, p.quoted_on)
    if before == (item, category, price, source, quoted):
        return
    old_price = p.unit_price
    p.item, p.category, p.unit_price, p.source, p.quoted_on = item, category, price, source, quoted
    p.updated_by, p.updated_at = who.user, utcnow()
    await module.audit.record(
        s,
        "price.changed",
        kind="price",
        entity_id=str(p.id),
        title=item,
        summary=f"Changed {item}"
        + (f": ${old_price:,.2f} → ${price:,.2f}" if old_price != price else ""),
    )


async def delete_price(s: AsyncSession, who: Principal, p: Price) -> None:
    _no_ai(who)
    await s.delete(p)
    await module.audit.record(
        s,
        "price.deleted",
        kind="price",
        entity_id=str(p.id),
        title=p.item,
        summary=f"Removed {p.item} (${p.unit_price:,.2f}) from the price list",
    )


def engine_prices(found: list[Price]) -> list[engine.Price]:
    return [
        engine.Price(
            p.item,
            p.category,
            float(p.unit_price),
            p.source,
            str(p.quoted_on) if p.quoted_on else None,
        )
        for p in found
    ]


# ---- the forecast's inputs -----------------------------------------------------------------


async def _service_rows(key: str, *args: Any, **kw: Any) -> list[dict[str, Any]] | None:
    fn = module.services.get(key)
    if fn is None:
        return None
    rows: list[dict[str, Any]] = await fn(*args, **kw)
    return rows


async def current(s: AsyncSession, today: dt.date | None = None) -> tuple[Forecast, Assumptions]:
    """Today's forecast for the current viewer."""
    a = await assumptions(s)
    price_list = engine_prices(await prices(s))
    fleet = await _service_rows("assets.fleet", in_use_days=a.in_use_days)
    people = await _service_rows("reports.rows", a.hr_dataset)
    purchases = await _service_rows("reports.rows", a.purchases_dataset)
    result = engine.forecast(
        fleet=fleet,
        people=people or None,
        purchases=purchases or None,
        prices=price_list,
        assumptions=a,
        today=today or dt.date.today(),
        excluded={x.asset_id: (x.reason, x.excluded_by) for x in await exclusions(s)},
        skips=await _skips_by_asset(s),
    )
    return result, a


# ---- computers left out by a person ------------------------------------------------------


async def exclusions(s: AsyncSession) -> list[Exclusion]:
    return list((await s.execute(select(Exclusion).order_by(Exclusion.name))).scalars())


async def exclude(s: AsyncSession, who: Principal, name: str, reason: str) -> Exclusion:
    """Leave one computer out of the budget, with why. Found by its name in Assets."""
    _no_ai(who)
    name, reason = " ".join(name.split()), " ".join(reason.split())[:500]
    if not name:
        raise BudgetError("Which computer? Give its name as Assets shows it.")
    if not reason:
        raise BudgetError("Say why it's left out (e.g. “controls the press, replaced with it”).")
    fleet = await _service_rows("assets.fleet") or []
    found = [d for d in fleet if str(d.get("name") or "").lower() == name.lower()]
    if not found:
        raise BudgetError(f"No device named {name} in Assets.")
    if len(found) > 1:
        raise BudgetError(f"{len(found)} devices are named {name} in Assets: merge them first.")
    asset_id, shown = str(found[0].get("id") or ""), str(found[0].get("name") or name)
    if await s.scalar(select(Exclusion).where(Exclusion.asset_id == asset_id)):
        raise BudgetError(f"{shown} is already left out.")
    x = Exclusion(asset_id=asset_id, name=shown, reason=reason, excluded_by=who.user)
    s.add(x)
    await s.flush()
    await module.audit.record(
        s,
        "exclusion.added",
        kind="exclusion",
        entity_id=asset_id,
        title=shown,
        summary=f"Left {shown} out of the budget: {reason}",
    )
    return x


async def include(s: AsyncSession, who: Principal, x: Exclusion) -> None:
    """Bring a computer a person left out back into the budget."""
    _no_ai(who)
    await s.delete(x)
    await module.audit.record(
        s,
        "exclusion.removed",
        kind="exclusion",
        entity_id=x.asset_id,
        title=x.name,
        summary=f"Brought {x.name} back into the budget",
    )


# ---- replacements a person skipped to next year ---------------------------------------------


async def skips(s: AsyncSession, year: int | None = None) -> list[Skip]:
    q = select(Skip).order_by(Skip.name)
    if year is not None:
        q = q.where(Skip.year == year)
    return list((await s.execute(q)).scalars())


async def _skips_by_asset(s: AsyncSession) -> dict[str, list[tuple[int, str, str, str]]]:
    out: dict[str, list[tuple[int, str, str, str]]] = {}
    for x in await skips(s):
        out.setdefault(x.asset_id, []).append((x.year, x.reason, x.skipped_by, x.due))
    return out


async def skip(s: AsyncSession, who: Principal, asset_id: str, reason: str) -> Skip:
    """Skip one computer on this year's replacement list to next year, with why. It's kept
    for the budget year it was skipped from; the year after, it's on the list again."""
    _no_ai(who)
    reason = " ".join(reason.split())[:500]
    if not asset_id:
        raise BudgetError("Pick the computer to skip from the replacement list.")
    if not reason:
        raise BudgetError(
            "Say why it can wait a year (e.g. “light use, replaced with the line in 2028”)."
        )
    f, _ = await current(s)
    year = f.plan_start or dt.date.today().year
    listed = {c.id: c for c in [*f.must_replace, *f.early]}
    if asset_id not in listed:
        if any(c.id == asset_id for c in f.skipped):
            raise BudgetError(f"That computer is already skipped to {year + 1}.")
        raise BudgetError(
            "That computer isn't on the replacement list any more (the list changes with the "
            "fleet and the plan). Pick one that's listed."
        )
    c = listed[asset_id]
    due = "; ".join([*c.reasons, *([c.early] if c.early else [])])[:500]
    x = Skip(asset_id=asset_id, name=c.name, year=year, reason=reason, due=due,
             skipped_by=who.user)  # fmt: skip
    s.add(x)
    await s.flush()
    await module.audit.record(
        s,
        "skip.added",
        kind="skip",
        entity_id=asset_id,
        title=c.name,
        summary=f"Skipped replacing {c.name} in {year} to {year + 1}: {reason}",
    )
    return x


async def unskip(s: AsyncSession, who: Principal, x: Skip) -> None:
    """Put a skipped computer back on its year's replacement list."""
    _no_ai(who)
    await s.delete(x)
    await module.audit.record(
        s,
        "skip.removed",
        kind="skip",
        entity_id=x.asset_id,
        title=x.name,
        summary=f"Put {x.name} back on the {x.year} replacement list",
    )


async def purchases_between(s: AsyncSession, first: dt.date, last: dt.date) -> float | None:
    """What purchase history says was spent from ``first`` to ``last``, both days included
    (None without history)."""
    a = await assumptions(s)
    rows = await _service_rows("reports.rows", a.purchases_dataset)
    if not rows:
        return None
    total = 0.0
    for row in rows:
        d, amount = engine.purchase_date(row), engine.spent(row)
        if d and first <= d <= last and amount is not None:
            total += amount
    return round(total, 2)


# ---- versions ------------------------------------------------------------------------------


async def versions(s: AsyncSession) -> list[Version]:
    return list((await s.execute(select(Version).order_by(Version.created_at.desc()))).scalars())


async def get_version(s: AsyncSession, version_id: int) -> Version | None:
    return await s.get(Version, version_id)


def snapshot(v: Version) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(v.snapshot)
    # Versions saved before the bands were relabelled (2026-10-19).
    bands: dict[str, dict[str, int]] = data["forecast"].get("bands", {})
    for cls, counts in bands.items():
        bands[cls] = {engine.RENAMED_BANDS.get(b, b): n for b, n in counts.items()}
    return data


async def save_version(s: AsyncSession, who: Principal, title: str) -> Version:
    """Freeze today's inputs and forecast under a name ("FY27 proposal")."""
    _no_ai(who)
    title = " ".join(title.split())[:200]
    if not title:
        raise BudgetError("Give the version a name, e.g. “FY27 proposal”.")
    result, a = await current(s)
    price_list = engine_prices(await prices(s))
    v = Version(
        title=title,
        status="saved",
        total=Decimal(str(result.total)) if result.total is not None else None,
        snapshot=json.dumps(
            {
                "assumptions": a.model_dump(),
                "prices": [asdict(p) for p in price_list],
                "forecast": result.as_dict(),
            }
        ),
        created_by=who.user,
    )
    s.add(v)
    await s.flush()
    await module.audit.record(
        s,
        "version.saved",
        kind="version",
        entity_id=str(v.id),
        title=title,
        summary=f"Saved “{title}”"
        + (f": ${result.total:,.2f}" if result.total is not None else ""),
    )
    return v


async def adopt(s: AsyncSession, who: Principal, v: Version) -> None:
    """Mark a version adopted (the one actual spend is tracked against)."""
    if who.via_ai:  # budget.approve is human-only; a second lock
        raise BudgetError("Adopting a budget is done by a person.")
    for other in await versions(s):
        if other.status == "adopted" and other.id != v.id:
            other.status, other.adopted_by, other.adopted_at = "saved", None, None
    v.status, v.adopted_by, v.adopted_at = "adopted", who.user, utcnow()
    await module.audit.record(
        s,
        "version.adopted",
        kind="version",
        entity_id=str(v.id),
        title=v.title,
        summary=f"Adopted “{v.title}”",
    )
    module.publish(
        "version_adopted",
        {"id": v.id, "title": v.title, "total": float(v.total) if v.total is not None else None},
    )


async def delete_version(s: AsyncSession, who: Principal, v: Version) -> None:
    _no_ai(who)
    if v.status == "adopted":
        raise BudgetError("This is the adopted version: adopt another one before deleting it.")
    await s.delete(v)
    await module.audit.record(
        s,
        "version.deleted",
        kind="version",
        entity_id=str(v.id),
        title=v.title,
        summary=f"Deleted “{v.title}”",
    )


# ---- proposals (AI suggests, a person approves) ------------------------------------------

PROPOSAL_KINDS = {
    "price_add": "Add a price",
    "price_change": "Change a price",
    "price_remove": "Remove a price",
    "assumptions": "Change the assumptions",
}
ASSUMPTION_LABELS = {
    "laptop_kinds": "Device kinds that are laptops",
    "desktop_kinds": "Device kinds that are desktops",
    "in_use_days": "In use if seen within (days)",
    "replace_rates": "Share replaced a year, by age (%)",
    "must_replace_age": "Must replace at (years old)",
    "unsupported_os": "Operating systems out of support",
    "upgradeable_models": "Models that can be upgraded in place (when hardware isn't known)",
    "equipment_models": "Models left out (their own equipment)",
    "seat_rule": "Seat rule",
    "active_status": "Status of current employees",
    "growth_years": "Years of hiring history to average",
    "kit_replacement_pct": "Peripherals wearing out (%)",
    "contingency_pct": "Contingency (%)",
    "steady_years": "Replacement cycle (years)",
    "low_factor": "Low scenario (share of expected failures)",
    "high_factor": "High scenario (share of expected failures)",
    "hr_dataset": "HR dataset",
    "purchases_dataset": "Purchase-history dataset",
    "plan": "Five-year plan",
    "plan_amount": "Yearly amount (Yearly amount plan)",
    "reuse_pct": "Leavers' computers reused (%)",
    "price_rise_pct": "Prices rise a year (%)",
    "lease_markup_pct": "A lease costs more by (%)",
}


def proposal_data(p: Proposal) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(p.data)
    return data


def _note(note: str) -> str:
    note = " ".join(note.split())[:1000]
    if not note:
        raise BudgetError("Say why and where it comes from (e.g. “HP quote Q123, 25 Sep 2026”).")
    return note


async def _propose(
    s: AsyncSession,
    who: Principal,
    kind: str,
    data: dict[str, Any],
    note: str,
    price_id: int | None = None,
) -> Proposal:
    p = Proposal(
        kind=kind, price_id=price_id, data=json.dumps(data), note=_note(note), proposed_by=who.user
    )
    s.add(p)
    await s.flush()
    item = f" ({data['item']})" if "item" in data else ""
    await module.audit.record(
        s,
        "proposal.made",
        kind="proposal",
        entity_id=str(p.id),
        title=PROPOSAL_KINDS[kind],
        summary=f"Proposed: {PROPOSAL_KINDS[kind].lower()}{item}",
    )
    return p


async def _price(s: AsyncSession, price_id: int) -> Price:
    found = await s.get(Price, price_id)
    if found is None:
        raise BudgetError(f"There's no price with id {price_id} (it may have been removed).")
    return found


async def propose_price(
    s: AsyncSession,
    who: Principal,
    *,
    item: str,
    category: str,
    unit_price: str,
    source: str = "",
    quoted_on: str = "",
    note: str,
    price_id: int | None = None,
) -> Proposal:
    """Propose a new price, or (with ``price_id``) a change to one; checked like a save."""
    item, category, price, source, quoted = _price_values(
        item, category, unit_price, source, quoted_on
    )
    if price_id is not None:
        await _price(s, price_id)
    data = {
        "item": item,
        "category": category,
        "unit_price": str(price),
        "source": source,
        "quoted_on": str(quoted) if quoted else "",
    }
    kind = "price_change" if price_id is not None else "price_add"
    return await _propose(s, who, kind, data, note, price_id)


async def propose_price_removal(
    s: AsyncSession, who: Principal, price_id: int, note: str
) -> Proposal:
    found = await _price(s, price_id)
    return await _propose(s, who, "price_remove", {"item": found.item}, note, price_id)


def merged_assumptions(current: Assumptions, changes: dict[str, Any]) -> Assumptions:
    """The assumptions with some keys changed (seat_rule and replace_rates part by part)."""
    unknown = sorted(set(changes) - set(Assumptions.model_fields))
    if unknown:
        raise BudgetError(
            f"Unknown assumption(s): {', '.join(unknown)}. "
            f"Known: {', '.join(Assumptions.model_fields)}."
        )
    values = current.model_dump()
    for key, value in changes.items():
        if key in ("seat_rule", "replace_rates") and isinstance(value, dict):
            values[key] = {**values[key], **value}
        else:
            values[key] = value
    try:
        return Assumptions.model_validate(values)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first["loc"])
        raise BudgetError(f"{where}: {first['msg']}") from None


async def propose_assumptions(
    s: AsyncSession, who: Principal, changes: dict[str, Any], note: str
) -> Proposal:
    """Propose changes to some assumptions (only the keys given); checked like a save."""
    current = await assumptions(s)
    before, after = current.model_dump(), merged_assumptions(current, changes).model_dump()
    changed = {k: after[k] for k in after if before[k] != after[k]}
    if not changed:
        raise BudgetError("Those are the assumptions already: nothing would change.")
    return await _propose(s, who, "assumptions", changed, note)


async def proposals(s: AsyncSession, status: str = "pending") -> list[Proposal]:
    q = select(Proposal).where(Proposal.status == status).order_by(Proposal.proposed_at)
    return list((await s.execute(q)).scalars())


async def get_proposal(s: AsyncSession, proposal_id: int) -> Proposal | None:
    return await s.get(Proposal, proposal_id)


def _decide(p: Proposal, who: Principal, status: str) -> None:
    if who.via_ai:  # budget.approve is human-only; a second lock
        raise BudgetError("A person approves or rejects proposed budget changes.")
    if p.status != "pending":
        raise BudgetError(f"This proposal was already {p.status}.")
    p.status, p.decided_by, p.decided_at = status, who.user, utcnow()


async def approve_proposal(s: AsyncSession, who: Principal, p: Proposal) -> None:
    """Apply a proposal as the person approving it (the change is audited as theirs)."""
    _decide(p, who, "approved")
    data = proposal_data(p)
    if p.kind == "price_add":
        await add_price(s, who, **data)
    elif p.kind == "price_change":
        assert p.price_id is not None
        await update_price(s, who, await _price(s, p.price_id), **data)
    elif p.kind == "price_remove":
        assert p.price_id is not None
        await delete_price(s, who, await _price(s, p.price_id))
    else:
        await save_assumptions(s, who, merged_assumptions(await assumptions(s), data))
    await module.audit.record(
        s,
        "proposal.approved",
        kind="proposal",
        entity_id=str(p.id),
        title=PROPOSAL_KINDS[p.kind],
        summary=f"Approved a change proposed by {p.proposed_by}",
    )


async def reject_proposal(s: AsyncSession, who: Principal, p: Proposal) -> None:
    _decide(p, who, "rejected")
    await module.audit.record(
        s,
        "proposal.rejected",
        kind="proposal",
        entity_id=str(p.id),
        title=PROPOSAL_KINDS[p.kind],
        summary=f"Rejected a change proposed by {p.proposed_by}",
    )


# ---- services ------------------------------------------------------------------------------


@module.service("dashboards")
async def dashboards() -> list[dict[str, str]]:
    """Budget's dashboard, for the Reports page (only for people who may see it)."""
    p = module.current_principal()
    if p is None or not p.has("budget.read"):
        return []
    return [
        {
            "title": "Device budget",
            "url": module.url("/"),
            "description": "The next 12 months of computers and peripherals: replacements, "
            "new seats, scenarios and what a steady policy would cost.",
        }
    ]


@module.service("waiting")
async def waiting() -> list[dict[str, Any]]:
    """What's waiting for the person looking (core's *Waiting for you*): proposals from AI
    for those who approve them."""
    who = module.current_principal()
    if who is None or not who.has("budget.approve"):
        return []
    async with module.session() as s:
        n = len(await proposals(s))
    return [{"title": f"{n} proposed budget change{'s' if n != 1 else ''} to approve",
             "url": "/budget/proposals", "count": n}]  # fmt: skip
