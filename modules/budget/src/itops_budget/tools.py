"""Budget MCP tools (exposed as ``budget_<name>``).

Tools run *via AI* as the connected user. They read the price list, the assumptions and the
forecast's counts; they never change a figure directly: the ``propose_*`` tools store
proposals, checked exactly like a real save, that a person approves or rejects on the
Assumptions and prices page (``budget.approve`` is human-only). The forecast's amounts are
shown on the Budget page only.
"""

from __future__ import annotations

from typing import Any

from itops.sdk import Principal
from itops_budget import service
from itops_budget.app import module
from itops_budget.engine import PRICE_ALIASES, PRICE_CATEGORIES
from itops_budget.models import Proposal


def _who() -> Principal:
    p = module.current_principal()
    if p is None:
        raise PermissionError("not signed in")
    return p


def _proposal(p: Proposal) -> dict[str, Any]:
    return {
        "proposal": p.id,
        "status": p.status,
        "what": service.PROPOSAL_KINDS[p.kind],
        "price_id": p.price_id,
        "values": service.proposal_data(p),
        "note": p.note,
        "proposed_by": p.proposed_by,
        "review_url": module.url("/proposals"),
    }


def _proposed(p: Proposal) -> dict[str, Any]:
    return {**_proposal(p), **module.review("/proposals", "the proposal")}


@module.tool(permission="budget.read")
async def forecast_counts() -> dict[str, Any]:
    """The device budget for next year and its five-year plan, without money: computers in
    use by class and age band, the replacement list (must be replaced and why, replaced early
    by the plan by name, and those a person skipped to next year with why; skipping is done
    by a person on the Budget page), how many are upgraded in
    place instead (unsupported system, upgradeable model), computers left out of the budget
    and why (their own equipment, or excluded by a person), expected replacements, net new
    seats a year (low/expected/high) from hiring history, spares, the chosen plan (minimum,
    even, cycle, amount or lease) with each year's computers (must go, fail, replaced early,
    reused, bought or leased, and how many are 7+ years old after), and what would make the
    forecast firmer. Amounts are only shown on the Budget page."""
    async with module.session() as s:
        f, a = await service.current(s)
    return {
        "computers_in_use": f.computers_in_use,
        "by_class": f.by_class,
        "by_age": f.bands,
        "spares": f.spares,
        "not_seen_lately": f.not_seen,
        "must_replace": {
            "count": len(f.must_replace),
            "rule": f"{a.must_replace_age}+ years old, or running {', '.join(a.unsupported_os)} "
            "on hardware that can't take a supported system (Assets' OS readiness; when it "
            "isn't known, a model not in upgradeable_models)",
            "computers": [{"computer": c.name, "why": c.reasons} for c in f.must_replace],
        },
        "replaced_early": [{"computer": c.name, "why": c.early} for c in f.early],
        "skipped_to_next_year": [
            {
                "computer": c.name,
                "was_due": c.skipped_due,
                "why": c.skipped,
                "skipped_by": c.skipped_by,
            }
            for c in f.skipped
        ],
        "upgrade_in_place": {
            "count": len(f.upgrades),
            "judged_by_hardware": sum(1 for c in f.upgrades if c.judged == "hardware"),
            "judged_by_model": sorted({c.model for c in f.upgrades if c.judged == "model"}),
        },
        "left_out": [{"computer": c.name, "model": c.model, "why": c.left_out} for c in f.left_out],
        "lines": [
            {"line": x.label, "count": x.count, "basis": x.basis}
            for x in f.lines
            if x.key != "contingency"
        ],
        "plan": next(
            (
                {
                    "plan": p["name"],
                    "years": [
                        {
                            "year": y["year"],
                            "must_replace": round(y["must"], 1),
                            "fail_or_cant_keep_up": round(y["failures"], 1),
                            "replaced_early": round(y["early"], 1),
                            "reused": round(y["reused"], 1),
                            "leased" if p["key"] == "lease" else "bought": round(y["bought"], 1),
                            "seven_plus_years_old_after": round(y["old"], 1),
                        }
                        for y in p["years"]
                    ],
                    "other_plans": [q["name"] for q in f.plans if q["key"] != p["key"]],
                }
                for p in f.plans
                if p["key"] == f.plan
            ),
            None,
        ),
        "net_new_seats_a_year": f.net_seats,
        "hiring_history": f.growth,
        "people_with_a_seat_today": f.predicted_seats,
        "what_would_firm_it_up": [g for g in f.gaps if "price" not in g.lower()],
        "page": module.absolute_url("/budget/"),
    }


@module.tool(permission="budget.read")
async def list_prices() -> list[dict[str, Any]]:
    """The budget's price list: each item with its id, what it's for (a computer class:
    standard_laptop, standard_desktop, engineering_laptop, engineering_desktop; or
    kit: one item of what each new seat gets), unit price in dollars, source and quote date.
    Several prices for one class are averaged; kit items are added up. Older entries say
    laptop or desktop: those are the standard ones."""
    async with module.session() as s:
        found = await service.prices(s)
    return [
        {
            "id": p.id,
            "item": p.item,
            "category": p.category,
            "unit_price": float(p.unit_price),
            "source": p.source,
            "quoted_on": str(p.quoted_on) if p.quoted_on else None,
        }
        for p in found
    ]


@module.tool(permission="budget.read")
async def get_assumptions() -> dict[str, Any]:
    """The forecast's assumptions, by key: which device kinds are laptops/desktops,
    in_use_days, replace_rates (% replaced a year per age band), must_replace_age,
    unsupported_os, upgradeable_models (models whose hardware runs a supported system: on an
    unsupported one they're upgraded, not replaced; only used when Assets has no hardware
    for a computer: its OS readiness decides otherwise), equipment_models (models of PCs that
    are their own equipment, e.g. machine controllers: left out of the budget), seat_rule
    (who gets a computer: anyone matching any of pay_types, dept_groups, departments,
    positions), kit_replacement_pct, contingency_pct, steady_years, low_factor/high_factor
    (fractions, 0.75 = 75%), and the Reports dataset keys. Percentages are 0-100 unless noted."""
    async with module.session() as s:
        a = await service.assumptions(s)
    return a.model_dump()


@module.tool(permission="budget.read")
async def list_proposals() -> list[dict[str, Any]]:
    """Proposed budget changes waiting for a person to approve or reject, oldest first.
    Check here before proposing something that may already be waiting."""
    async with module.session() as s:
        return [_proposal(p) for p in await service.proposals(s)]


@module.tool(permission="budget.write", writes=True)
async def propose_price(
    item: str,
    category: str,
    unit_price: str,
    note: str,
    source: str = "",
    quoted_on: str = "",
    id: int | None = None,
) -> dict[str, Any]:
    """Propose a NEW price list entry, or a change to one (give its `id` from list_prices).
    Nothing changes until a person approves it; tell the user the returned `review_url`.
    You cannot approve.

    `category`: a computer class (standard_laptop, standard_desktop, engineering_laptop,
    engineering_desktop) or kit (one item each new seat gets: a monitor, dock,
    keyboard and mouse; two monitors = two kit entries). `unit_price` in dollars, e.g.
    "1332.84". `source`: e.g. "HP quote Q3100327454"; `quoted_on`: YYYY-MM-DD. `note`
    says why and where it comes from, for the person who reviews it."""
    category = PRICE_ALIASES.get(category, category)
    if category not in PRICE_CATEGORIES:
        raise service.BudgetError(f"category is one of: {', '.join(PRICE_CATEGORIES)}.")
    async with module.session() as s:
        p = await service.propose_price(
            s,
            _who(),
            item=item,
            category=category,
            unit_price=unit_price,
            source=source,
            quoted_on=quoted_on,
            note=note,
            price_id=id,
        )
        await s.commit()
        return _proposed(p)


@module.tool(permission="budget.write", writes=True)
async def propose_price_removal(id: int, note: str) -> dict[str, Any]:
    """Propose removing an entry from the price list (its `id` from list_prices). Nothing
    changes until a person approves it; tell the user the returned `review_url`."""
    async with module.session() as s:
        p = await service.propose_price_removal(s, _who(), id, note)
        await s.commit()
        return _proposed(p)


@module.tool(permission="budget.write", writes=True)
async def propose_assumptions(changes: dict[str, Any], note: str) -> dict[str, Any]:
    """Propose changes to the forecast's assumptions. `changes` holds only the keys that
    change (see get_assumptions); seat_rule and replace_rates may give just the parts that
    change, e.g. {"seat_rule": {"dept_groups": ["Engineering"]}, "contingency_pct": 15}.
    Nothing changes until a person approves it; tell the user the returned `review_url`.
    `note` says why, for the person who reviews it."""
    async with module.session() as s:
        p = await service.propose_assumptions(s, _who(), changes, note)
        await s.commit()
        return _proposed(p)
