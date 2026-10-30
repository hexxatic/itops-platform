"""Budget pages (server-rendered, core components only) and the form actions behind them.

Pages live at /budget/...; actions are POSTs under /api/budget/... that redirect back to a
page. Problems come back as ``?error=...`` and a failed save keeps the form.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from itops.sdk import KeptForm, Principal
from itops.sdk.export import Column, ExportProblem, export_response, money
from itops_budget import service
from itops_budget.app import module
from itops_budget.engine import (
    BANDS,
    CLASS_LABELS,
    CLASS_NAMES,
    PRICE_CATEGORIES,
    PRICE_LABELS,
    Assumptions,
    SeatRule,
    plural,
)
from itops_budget.models import Exclusion, Price, Proposal, Skip, Version
from itops_budget.plan import OLD, PLANS
from itops_budget.service import BudgetError

CanRead = Annotated[Principal, Depends(module.require("budget.read"))]
CanWrite = Annotated[Principal, Depends(module.require("budget.write"))]
CanApprove = Annotated[Principal, Depends(module.require("budget.approve"))]
Text = Annotated[str, Form()]

BASIS = {
    "data": ("From data", "success"),
    "partial": ("Partly estimated", "warning"),
    "estimate": ("Estimate", "neutral"),
}
STATUS = {"saved": ("Saved", "neutral"), "adopted": ("Adopted", "success")}


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


def _back(url: str, error: str | None = None) -> RedirectResponse:
    url, _, fragment = url.partition("#")
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url + (f"#{fragment}" if fragment else ""), status_code=303)


async def _form_back(request: Request, url: str, error: str) -> RedirectResponse:
    """``module.forms.back``, keeping a ``#part`` of the page after the query it adds."""
    url, _, fragment = url.partition("#")
    r = await module.forms.back(request, url, error)
    if fragment:
        r.headers["location"] += f"#{fragment}"
    return r


def _when(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _shown(amount: float | None) -> str:
    return money(amount) if amount is not None else "Needs prices"


def _count(n: float | None) -> str:
    if n is None:
        return ""
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.1f}"


def _quarter_labels(f: dict[str, Any]) -> list[str]:
    """The plan year's calendar quarters, or (a version saved before plans) the next four
    quarters of a rolling 12 months."""
    if f.get("plan_start"):
        return [f"Q{q} {f['plan_start']}" for q in range(1, 5)]
    return _quarters(dt.date.fromisoformat(f["today"]))


def _chosen(f: dict[str, Any]) -> dict[str, Any] | None:
    return next((p for p in f.get("plans", []) if p["key"] == f.get("plan")), None)


def _computers_cost(y: dict[str, Any], lease: bool) -> float:
    if lease:
        return float(y["lease"])
    return float(y["must_cost"] + y["failure_cost"] + y["early_cost"] - y["reuse_saving"])


def _peak(p: dict[str, Any]) -> dict[str, Any]:
    return max(p["years"], key=lambda y: y["total"])


def _plan_note(p: dict[str, Any], a: Assumptions, size: int) -> str:
    years = p["years"]
    first, last = years[0], years[-1]
    over = [y for y in years if y["over"] > 0]
    match p["key"]:
        case "minimum":
            return f"Cheapest in {first['year']}; {_peak(p)['year']} is the highest year."
        case "even":
            return (
                f"No year over {money(_peak(p)['total'])}: computers that come due are "
                "replaced early."
            )
        case "cycle":
            return (
                f"About {size / max(1, a.steady_years):,.0f} computers a year; "
                f"{plural(last['old'], 'computer')} {OLD}+ years old by {last['year']}."
            )
        case "amount":
            if a.plan_amount <= 0:
                return "Set a yearly amount to use this plan."
            if over:
                return (
                    f"Over {money(a.plan_amount)} in {plural(len(over), 'year')}: failures and "
                    "computers that must go are still replaced."
                )
            return f"{money(a.plan_amount)} a year."
        case _:
            return (
                f"Payments grow to {money(last['lease'])} a year by {last['year']}; nothing is "
                "owned at the end."
            )


def plan_context(f: dict[str, Any], a: Assumptions) -> dict[str, Any] | None:
    """The five-year plan card (ADR 0020), or None for a version saved before plans or a
    forecast without prices."""
    chosen = _chosen(f)
    if chosen is None:
        return None
    lease = chosen["key"] == "lease"
    years = chosen["years"]
    size = int(f["computers_in_use"])
    minimum = next(p for p in f["plans"] if p["key"] == "minimum")
    return {
        "key": chosen["key"],
        "name": chosen["name"],
        "about": chosen["about"],
        "start": f["plan_start"],
        "total": money(chosen["total"]),
        "chart": {
            "labels": [str(y["year"]) for y in years],
            "series": [
                {
                    "name": "Lease payments" if lease else "Computers",
                    "values": [round(_computers_cost(y, lease), 2) for y in years],
                },
                {
                    "name": "New seats, peripherals, contingency",
                    "values": [
                        round(y["seats"] + y["peripherals"] + y["contingency"], 2) for y in years
                    ],
                },
            ],
            "title": f"The {chosen['name']} plan by year",
        },
        "columns": [
            ("year", "Year"),
            ("must", "Must replace"),
            ("failures", "Fail or can't keep up"),
            ("early", "Replaced early"),
            ("reused", "Reused"),
            ("bought", "Leased" if lease else "Bought"),
            ("computers", "Lease payments" if lease else "Computers"),
            ("total", "Total"),
            ("old", f"{OLD}+ years old after"),
        ],
        "rows": [
            {
                "year": str(y["year"]),
                "must": _count(round(y["must"])),
                "failures": _count(round(y["failures"])),
                "early": _count(round(y["early"])),
                "reused": _count(round(y["reused"])),
                "bought": _count(round(y["bought"])),
                "computers": money(_computers_cost(y, lease)),
                "total": money(y["total"]),
                "old": _count(round(y["old"])),
            }
            for y in years
        ],
        "compare_columns": [
            ("name", "Plan"),
            ("first", str(years[0]["year"])),
            ("peak", "Highest year"),
            ("five", "Five years"),
            ("old", f"{OLD}+ years old at the end"),
            ("note", "Worth knowing"),
        ],
        "compare": [
            {
                "name": {"badge": p["name"], "tone": "info"}
                if p["key"] == chosen["key"]
                else p["name"],
                "first": money(p["years"][0]["total"]),
                "peak": f"{money(_peak(p)['total'])} ({_peak(p)['year']})",
                "five": money(p["total"]),
                "old": _count(round(p["years"][-1]["old"])),
                "note": _plan_note(p, a, size),
            }
            for p in f["plans"]
        ],
        "minimum_total": money(minimum["total"]),
    }


def _quarters(today: dt.date) -> list[str]:
    """Labels for the next four quarters of a rolling 12 months from this month."""
    out: list[str] = []
    for q in range(4):
        first = dt.date(
            today.year + (today.month - 1 + 3 * q) // 12, (today.month - 1 + 3 * q) % 12 + 1, 1
        )
        last_m = (first.month - 1 + 2) % 12 + 1
        last_y = first.year + (first.month - 1 + 2) // 12
        out.append(f"{first:%b %Y} to {dt.date(last_y, last_m, 1):%b %Y}")
    return out


def forecast_context(f: dict[str, Any], a: Assumptions) -> dict[str, Any]:
    """What the forecast parts template shows, from a forecast as a dict (today's, or a
    saved version's snapshot) and the assumptions it was worked out with."""
    plan = plan_context(f, a)
    lines = [
        {
            "label": x["label"],
            "count": _count(x["count"]),
            "amount": x.get("shown")
            or ("No purchase" if x["key"] == "upgrade" else _shown(x["amount"])),
            "basis": {"badge": BASIS[x["basis"]][0], "tone": BASIS[x["basis"]][1]},
            "note": x["note"],
        }
        for x in f["lines"]
    ]
    total = f["total"]
    upgrades = f.get("upgrades", [])
    estimated = all(x["basis"] == "estimate" for x in f["lines"])
    tiles = [
        {
            "label": f"{f['plan_start']} budget" if f.get("plan_start") else "Next 12 months",
            "value": _shown(total),
            "basis": "estimate" if estimated else "partial",
            "basis_note": "Counts come from Assets and HR; rates and prices from the assumptions "
            "and the price list.",
        },
        {
            "label": "Computers in use",
            "value": f"{f['computers_in_use']:,}",
            "basis": "data",
            "basis_note": ", ".join(
                f"{n:,} {str(CLASS_LABELS.get(c) or c).lower()}"
                for c, n in f["by_class"].items()
                if n
            ),
        },
        {
            "label": "Must replace",
            "value": f"{len(f['must_replace']):,}",
            "basis": "data",
            "basis_note": "Too old, or an unsupported operating system"
            + (f"; {len(upgrades):,} more upgraded in place" if upgrades else ""),
        },
        {
            "label": f"Five years ({plan['name']} plan)",
            "value": plan["total"],
            "basis": "estimate",
            "basis_note": f"Minimum plan: {plan['minimum_total']}",
        }
        if plan
        else {
            "label": "Net new seats a year",
            "value": _count(f["net_seats"]["expected"]),
            "basis": "partial" if f["growth"] else "estimate",
            "basis_note": "From hiring history and the seat rule",
        },
    ]
    scenarios = [
        {
            "label": "Low",
            "value": _shown(f["scenarios"]["low"]),
            "basis": "estimate",
            "basis_note": "Fewer failures, the slowest year of growth",
        },
        {"label": "Expected", "value": _shown(f["scenarios"]["expected"]), "basis": "estimate"},
        {
            "label": "High",
            "value": _shown(f["scenarios"]["high"]),
            "basis": "estimate",
            "basis_note": "More failures, the fastest year of growth",
        },
    ]
    steady = f["steady"]
    steady_tiles = [
        {
            "label": "Replace as needed (next 12 months)",
            "value": _shown(steady["as_needed"]),
            "basis": "estimate",
            "basis_note": "Must replace plus replacements as needed",
        },
        {
            "label": f"Steady: every computer at {steady['years']} years",
            "value": _shown(steady["yearly"]),
            "change": f"{_count(steady['per_year'])} computers a year",
            "basis": "estimate",
        },
    ]
    replacing = replacement_list(f)
    year = f.get("plan_start")
    fails = next((x["count"] for x in f["lines"] if x["key"] == "replacements"), 0) or 0
    actuals = f["actuals"]
    return {
        "tiles": tiles,
        "lines": lines,
        "line_columns": [
            ("label", "Line"),
            ("count", "Computers or kits"),
            ("amount", "Amount"),
            ("basis", "Basis"),
            ("note", "How it's worked out"),
        ],
        "year_title": (
            f"{f['plan_start']}: the {plan['name']} plan" if plan else "The next 12 months"
        ),
        "plan": plan,
        "quarters": [
            {"label": q, "value": money(v)}
            for q, v in zip(_quarter_labels(f), f["quarters"], strict=False)
        ],
        "scenarios": scenarios,
        "steady_tiles": steady_tiles,
        "turning": {
            "labels": [str(t["year"]) for t in steady["turning"]],
            "series": [{"name": "Computers", "values": [t["count"] for t in steady["turning"]]}],
            "title": f"Computers reaching {steady['years']} years old, by year "
            f"(this year includes those already past it)",
        },
        "unknown_age": steady["unknown_age"],
        "ages": {
            "labels": list(BANDS),
            "series": [
                {"name": CLASS_LABELS.get(c, c), "values": [f["bands"][c][b] for b in BANDS]}
                for c in f["bands"]
                if any(f["bands"][c].values())
            ],
        },
        "growth": {
            "labels": [str(g["year"]) for g in f["growth"]],
            "series": [
                {"name": "Hired", "values": [g["hires"] for g in f["growth"]]},
                {"name": "Left", "values": [g["terms"] for g in f["growth"]]},
            ],
            "rows": [
                {
                    "year": str(g["year"]),
                    "hires": g["hires"],
                    "terms": g["terms"],
                    "net": f"{g['net']:+d}",
                }
                for g in f["growth"]
            ],
        },
        "net_seats": f["net_seats"],
        "predicted_seats": f["predicted_seats"],
        "computers_in_use": f["computers_in_use"],
        "spares": f["spares"],
        "actuals": {
            "labels": [str(y["year"]) for y in actuals],
            "series": [
                {"name": "Computers", "values": [y["computers"] for y in actuals]},
                {"name": "Peripherals", "values": [y["peripherals"] for y in actuals]},
                {"name": "Other", "values": [y["other"] for y in actuals]},
            ],
        },
        "replacements_last_year": f["replacements_last_year"],
        "replacing": replacing[:LIST_SHOWN],
        "replacing_more": max(0, len(replacing) - LIST_SHOWN),
        "replacing_total": len(replacing),
        "replacing_must": len(f["must_replace"]),
        "replacing_title": f"Replacement list for {year}" if year else "Replacement list",
        "replacing_columns": [
            ("name", "Computer"),
            ("list", "List"),
            ("class", "Class"),
            ("model", "Model"),
            ("age", "Age"),
            ("os", "Operating system"),
            ("why", "Why"),
        ],
        "fails": _count(fails) if fails else "",
        "forecast": f,
        "year": year,
        "skipped": [
            {
                "name": _named(c),
                "due": _why(c),
                "why": c["skipped"],
                "by": c["skipped_by"],
            }
            for c in f.get("skipped", [])
        ],
        "skipped_columns": [
            ("name", "Computer"),
            ("due", "Why it was due"),
            ("why", "Why it can wait"),
            ("by", "Skipped by"),
        ],
        "gaps": [{"text": g, "link": f.get("gap_links", {}).get(g)} for g in f["gaps"]],
        "left_out": [
            {
                "name": {"text": c["name"], "href": c["url"]} if c["url"] else c["name"],
                "model": c["model"],
                "os": c["os"],
                "why": c.get("left_out") or "Its own equipment",
                "by": c.get("left_out_by") or "Models list (assumptions)",
            }
            for c in f.get("left_out", [])
        ],
        "left_out_columns": [
            ("name", "Computer"),
            ("model", "Model"),
            ("os", "Operating system"),
            ("why", "Why"),
            ("by", "Left out by"),
        ],
    }


LIST_SHOWN = 100


def _named(c: dict[str, Any]) -> Any:
    return {"text": c["name"], "href": c["url"]} if c["url"] else c["name"]


def _why(c: dict[str, Any]) -> str:
    """Why a computer is on the replacement list (for a skipped one: why it was)."""
    return "; ".join([*c["reasons"], *([c["early"]] if c.get("early") else [])]) or c.get(
        "skipped_due", ""
    )


def replacement_list(f: dict[str, Any]) -> list[dict[str, Any]]:
    """This year's replacements by name, from a forecast dict: the computers that must go
    (oldest first), then those the plan replaces early. Failures can't be named."""
    rows: list[dict[str, Any]] = []
    for c, kind in [
        *((c, ("Must go", "danger")) for c in f["must_replace"]),
        *((c, ("Early", "neutral")) for c in f.get("early", [])),
    ]:
        why = _why(c)
        if c.get("skipped_before"):
            why += f". {c['skipped_before']}"
        rows.append(
            {
                "id": c["id"],
                "name": _named(c),
                "list": {"badge": kind[0], "tone": kind[1]},
                "class": CLASS_NAMES.get(c["cls"], c["cls"]),
                "model": c["model"],
                "age": f"{c['age']:g} years" if c["age"] is not None else "Unknown",
                "os": c["os"],
                "why": why,
                "plain": {
                    "name": c["name"],
                    "list": kind[0],
                    "acquired": c["acquired"],
                    "age": c["age"],
                    "last_seen": c["last_seen"],
                },
            }
        )
    return rows


def _sentence(parts: list[str]) -> str:
    if len(parts) > 1:
        parts = [*parts[:-2], f"{parts[-2]} and {parts[-1]}"]
    text = ", ".join(parts)
    return text[:1].upper() + text[1:] + "." if text else ""


def summary_context(snap: dict[str, Any], adopted: Version | None) -> dict[str, Any]:
    """The one-page summary of a saved version (ADR 0018): the total, a few lines each with
    one plain sentence, what it buys and defers, and when the money goes. Generated from the
    snapshot, never typed. ``adopted`` is the adopted version to compare with, if another."""
    f = snap["forecast"]
    a = Assumptions.model_validate(snap["assumptions"])
    today = dt.date.fromisoformat(f["today"])
    by_key: dict[str, dict[str, Any]] = {x["key"]: x for x in f["lines"]}
    pct = a.contingency_pct
    systems = ", ".join(a.unsupported_os) or "an unsupported system"

    def n(key: str) -> float:
        return float((by_key.get(key) or {}).get("count") or 0)

    def amount(key: str) -> str:
        x = by_key.get(key)
        return (x.get("shown") or _shown(x["amount"])) if x else ""

    must, upgrades, repl, seats = n("must_replace"), n("upgrade"), n("replacements"), n("growth")
    rows: list[dict[str, Any]] = []
    if must:
        rows.append(
            {
                "what": f"Replace {plural(must, 'computer')}",
                "amount": amount("must_replace"),
                "why": f"They're {a.must_replace_age} or more years old, or run {systems}"
                + (" and can't be upgraded." if upgrades or a.upgradeable_models else "."),
            }
        )
    if upgrades:
        rows.append(
            {
                "what": f"Upgrade {plural(upgrades, 'computer')} in place",
                "amount": "No purchase",
                "why": f"They run {systems}, but the hardware can take a supported system: "
                "IT upgrades the computers we have.",
            }
        )
    rows.append(
        {
            "what": f"Replace about {plural(repl, 'more', 'more')} as they fail",
            "amount": amount("replacements"),
            "why": "From how often computers of each age fail or can't keep up.",
        }
    )
    chosen = _chosen(f)
    early, reused = n("early"), -n("reuse")
    if early:
        rows.append(
            {
                "what": f"Replace {plural(early, 'more computer')} early",
                "amount": amount("early"),
                "why": "The oldest computers, replaced before they fail so no year carries "
                f"a spike (the {chosen['name'] if chosen else ''} plan).",
            }
        )
    if reused:
        rows.append(
            {
                "what": f"Reuse {plural(reused, 'computer')} from people who leave",
                "amount": amount("reuse"),
                "why": "Given to someone whose computer is due, instead of buying one.",
            }
        )
    if "lease" in by_key:
        rows.append(
            {
                "what": f"Lease payments for {plural(n('lease'), 'computer')}",
                "amount": amount("lease"),
                "why": f"Leased, not bought: each the purchase price over {a.steady_years} "
                f"years plus {a.lease_markup_pct:g}%, until a lease is quoted.",
            }
        )
    rows.append(
        {
            "what": "New seats",
            "amount": amount("growth"),
            "why": (
                f"About {plural(seats, 'new person', 'new people')} a year who get a computer "
                "of their own, from hiring history, each with a computer and a kit."
                if seats
                else "No new seats expected beyond the spares on the shelf."
            ),
        }
    )
    rows.append(
        {
            "what": "Monitors, docks and peripherals",
            "amount": amount("peripherals"),
            "why": f"Replacing what wears out ({a.kit_replacement_pct:g}% of a kit per "
            "computer a year).",
        }
    )
    rows.append(
        {
            "what": f"Contingency ({pct:g}%)",
            "amount": amount("contingency"),
            "why": "For what nobody sees coming.",
        }
    )

    buys: list[str] = []
    if must:
        buys.append(f"{plural(must, 'computer')} that are too old or out of support replaced")
    if upgrades:
        buys.append(f"{plural(upgrades, 'computer')} moved to a supported system")
    if repl:
        buys.append(f"about {plural(repl, 'replacement')} for computers that fail")
    if early:
        buys.append(
            f"{plural(early, 'old computer')} replaced before "
            + ("it fails" if early == 1 else "they fail")
        )
    if seats:
        buys.append(f"computers for about {plural(seats, 'new person', 'new people')}")
    aging = sum(b[BANDS[2]] for b in f["bands"].values())
    unknown = sum(b[BANDS[4]] for b in f["bands"].values())
    defers: list[str] = []
    if f.get("skipped"):
        defers.append(
            f"{plural(len(f['skipped']), 'computer')} due this year skipped to "
            f"{(f.get('plan_start') or today.year) + 1}, listed below with why"
        )
    if f.get("left_out"):
        defers.append(
            f"{plural(len(f['left_out']), 'computer')} left out of this budget, listed below "
            "with why"
        )
    if aging:
        defers.append(f"{plural(aging, 'computer')} aged {BANDS[2].lower()}, kept unless they fail")
    if unknown:
        defers.append(f"{plural(unknown, 'computer')} with no purchase date, assumed fine")

    total = f["total"]
    labels = _quarter_labels(f)
    quarters: list[dict[str, Any]] = []
    if total is not None:
        # The must-replace computers are bought first; the rest is spread evenly.
        first = round((by_key["must_replace"]["amount"] or 0) * (1 + pct / 100), 2)
        rest = round((total - first) / 4, 2)
        values = [first + rest, rest, rest, round(total - first - 3 * rest, 2)]
        quarters = [{"label": q, "value": money(v)} for q, v in zip(labels, values, strict=True)]
    compare: list[str] = []
    if adopted is not None and adopted.total is not None:
        compare.append(f"Adopted now ({adopted.title}): {money(float(adopted.total))}")
    spent = [y for y in f["actuals"] if y["year"] < today.year]
    if spent:
        last = spent[-1]
        compare.append(
            f"Spent in {last['year']}: "
            + money(last["computers"] + last["peripherals"] + last["other"])
        )
    low, high = f["scenarios"]["low"], f["scenarios"]["high"]
    outlook: list[dict[str, Any]] = []
    spend_less = ""
    if chosen is not None:
        outlook = [{"label": str(y["year"]), "value": money(y["total"])} for y in chosen["years"]]
        minimum = next(p for p in f["plans"] if p["key"] == "minimum")
        m_first, m_last, m_peak = minimum["years"][0], minimum["years"][-1], _peak(minimum)
        spend_less = (
            "This is the least that can be spent: computers that must go and those that fail "
            "are always replaced."
            if chosen["key"] == "minimum"
            else f"The Minimum plan needs {money(m_first['total'])} in {m_first['year']}, but "
            f"{money(m_peak['total'])} in {m_peak['year']}, and leaves "
            f"{plural(m_last['old'], 'computer')} {OLD} or more years old by {m_last['year']}."
        )
    return {
        "plan_name": chosen["name"] if chosen else "",
        "outlook": outlook,
        "five_years": money(chosen["total"]) if chosen else "",
        "spend_less": spend_less,
        "headline": [
            {
                "label": (
                    f"The {f['plan_start']} device budget"
                    if f.get("plan_start")
                    else f"The 12 months from {labels[0].split(' to ')[0]}"
                ),
                "value": _shown(total),
                "change": " · ".join(compare) or None,
            }
        ],
        "rows": rows,
        "left_out": [
            {"name": c["name"], "why": c.get("left_out") or "Its own equipment"}
            for c in f.get("left_out", [])
        ],
        "replacing": [
            {"name": r["plain"]["name"], "list": r["list"], "model": r["model"], "why": r["why"]}
            for r in replacement_list(f)
        ],
        "skipped": [
            {"name": c["name"], "due": _why(c), "why": c["skipped"]} for c in f.get("skipped", [])
        ],
        "buys": _sentence(buys) or "Nothing beyond what's already there.",
        "defers": _sentence(defers) or "Nothing that's due.",
        "quarters": quarters,
        "range": (
            f"Could run from {money(low)} to {money(high)}, depending on how many computers "
            "fail and how many people are hired."
            if low is not None and high is not None
            else None
        ),
    }


# ---- pages ---------------------------------------------------------------------------------


@module.page(
    "/", template="index.html", title="Device budget", nav="Budget", permission="budget.read"
)
async def home(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        result, a = await service.current(s)
        saved = await service.versions(s)
        waiting = len(await service.proposals(s))
        excluded = await service.exclusions(s)
    context = forecast_context(result.as_dict(), a)
    can_write = _can("budget.write")
    context["replacements_href"] = "/budget/replacements"
    context["replacing_tiles"] = _replacing_tiles(result.as_dict())
    by_asset = {x.asset_id: x for x in excluded}
    shown = {c.id for c in result.left_out}
    for row, c in zip(context["left_out"], result.left_out, strict=True):
        x = by_asset.get(c.id)
        row["action"] = (
            {
                "action": f"/api/budget/exclusions/{x.id}/delete",
                "label": "Bring back",
                "variant": "ghost",
            }
            if x and can_write
            else ""
        )
    for x in excluded:  # left out by a person, but not counted as in use right now
        if x.asset_id not in shown:
            context["left_out"].append(
                {
                    "name": x.name,
                    "model": "",
                    "os": "",
                    "why": f"{x.reason} (not in use now)",
                    "by": x.excluded_by,
                    "action": {
                        "action": f"/api/budget/exclusions/{x.id}/delete",
                        "label": "Bring back",
                        "variant": "ghost",
                    }
                    if can_write
                    else "",
                }
            )
    context["left_out_columns"] = [*context["left_out_columns"], ("action", "")]
    kept = module.forms.recall(request)
    kept = kept if kept is not None and "reason" in kept else None
    return {
        "waiting": waiting,
        **context,
        "exclude": {
            "name": str(kept.get("name") or "") if kept else "",
            "reason": str(kept.get("reason") or "") if kept else "",
        },
        "versions": [
            {
                "title": {"text": v.title, "href": f"/budget/v/{v.id}"},
                "status": {"badge": STATUS[v.status][0], "tone": STATUS[v.status][1]},
                "total": money(v.total) if v.total is not None else "Needs prices",
                "by": v.created_by,
                "when": _when(v.created_at),
                "summary": {"text": "Summary", "href": f"/budget/v/{v.id}/summary"},
                # The adopted one can't be deleted: actual spend is tracked against it.
                "delete": {
                    "action": f"/api/budget/versions/{v.id}/delete",
                    "label": "Delete",
                    "variant": "ghost",
                    "confirm": f"Delete the saved version “{v.title}”?",
                }
                if can_write and v.status != "adopted"
                else "",
            }
            for v in saved
        ],
        "can_write": can_write,
        "plan_form": _plan_form(a, kept_plan(request)),
        "error": request.query_params.get("error"),
    }


def _replacing_tiles(f: dict[str, Any]) -> list[dict[str, Any]]:
    year = f.get("plan_start")
    return [
        {"label": "Must go", "value": f"{len(f['must_replace']):,}", "basis": "data",
         "basis_note": "Too old, or an operating system that's out of support"},
        {"label": "Replaced early", "value": f"{len(f.get('early', [])):,}", "basis": "estimate",
         "basis_note": "The oldest, by the chosen plan"},
        {"label": f"Skipped to {year + 1}" if year else "Skipped", "basis": "data",
         "value": f"{len(f.get('skipped', [])):,}", "basis_note": "Kept a year, with why"},
    ]  # fmt: skip


@module.page(
    "/replacements", template="replacements.html", title="Replacement list",
    permission="budget.read",
)  # fmt: skip
async def replacements_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        result, a = await service.current(s)
        skipped = await service.skips(s, result.plan_start or dt.date.today().year)
    f = result.as_dict()
    context = forecast_context(f, a)
    context["replacing"] = replacement_list(f)  # all of them on its own page
    context["replacing_more"] = 0
    _skip_actions(request, context, result.skipped, skipped, _can("budget.write"))
    return {**context, "error": request.query_params.get("error")}


def _skip_actions(
    request: Request,
    context: dict[str, Any],
    in_use: list[Any],
    skipped: list[Skip],
    can_write: bool,
) -> None:
    """Skip and Put back on today's replacement list, and the form that skips one."""
    year = context["year"] or dt.date.today().year

    def put_back(x: Skip) -> dict[str, str] | str:
        if not can_write:
            return ""
        return {"action": f"/api/budget/skips/{x.id}/delete", "label": "Put back",
                "variant": "ghost"}  # fmt: skip

    by_asset = {x.asset_id: x for x in skipped}
    for row, c in zip(context["skipped"], in_use, strict=True):
        x = by_asset.get(c.id)
        row["action"] = put_back(x) if x else ""
    shown = {c.id for c in in_use}
    for x in skipped:  # skipped, but not counted as in use right now
        if x.asset_id not in shown:
            context["skipped"].append(
                {
                    "name": x.name,
                    "due": x.due,
                    "why": f"{x.reason} (not in use now)",
                    "by": x.skipped_by,
                    "action": put_back(x),
                }
            )
    context["skipped_columns"] = [*context["skipped_columns"], ("action", "")]
    if not can_write:
        return
    for row in context["replacing"]:
        row["skip"] = {"text": "Skip", "href": f"/budget/replacements?skip={quote(row['id'])}#skip"}
    context["replacing_columns"] = [*context["replacing_columns"], ("skip", "")]
    kept = module.forms.recall(request)
    kept = kept if kept is not None and "asset_id" in kept else None
    context["skip_form"] = {
        "options": [("", "Pick a computer")]
        + [
            (r["id"], f"{r['plain']['name']} ({r['plain']['list'].lower()})")
            for r in replacement_list(context["forecast"])
        ],
        "selected": str(kept.get("asset_id") or "") if kept else request.query_params.get("skip"),
        "reason": str(kept.get("reason") or "") if kept else "",
        "next": year + 1,
    }


def kept_plan(request: Request) -> KeptForm | None:
    kept = module.forms.recall(request)
    return kept if kept is not None and "plan" in kept and "in_use_days" not in kept else None


def _plan_form(a: Assumptions, kept: KeptForm | None) -> dict[str, Any]:
    def value(name: str, fallback: Any) -> str:
        return str(kept.get(name) or "") if kept is not None else str(fallback)

    return {
        "plans": [(k, name) for k, (name, _) in PLANS.items()],
        "plan": value("plan", a.plan),
        "steady_years": value("steady_years", a.steady_years),
        "must_replace_age": value("must_replace_age", a.must_replace_age),
        "plan_amount": value("plan_amount", f"{a.plan_amount:.0f}" if a.plan_amount else ""),
        "reuse_pct": value("reuse_pct", f"{a.reuse_pct:g}"),
        "price_rise_pct": value("price_rise_pct", f"{a.price_rise_pct:g}"),
        "lease_markup_pct": value("lease_markup_pct", f"{a.lease_markup_pct:g}"),
    }


def _csv(values: list[str]) -> str:
    return ", ".join(values)


def _split(text: str) -> list[str]:
    return [v.strip() for v in text.replace("\n", ",").split(",") if v.strip()]


async def _choices(dataset: str, column: str) -> list[str]:
    """Values a dataset's column holds (shown next to the seat rule), for the viewer."""
    fn = module.services.get("reports.rows")
    if fn is None:
        return []
    rows: list[dict[str, Any]] = await fn(dataset)
    return sorted({str(r.get(column)) for r in rows if r.get(column)})


@module.page(
    "/proposals", template="proposals.html", title="Proposed changes", permission="budget.read"
)
async def proposals_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        a = await service.assumptions(s)
        by_id = {q.id: q for q in await service.prices(s)}
        waiting = await service.proposals(s)
    return {
        "proposals": [_proposal_view(x, a, by_id) for x in waiting],
        "can_approve": _can("budget.approve"),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/assumptions",
    template="assumptions.html",
    title="Budget assumptions",
    permission="budget.write",
)
async def assumptions_page(request: Request) -> dict[str, Any]:
    kept = module.forms.recall(request)
    # One page, two forms: the kept form is the assumptions' or a new price's.
    kept_price = kept if kept is not None and "unit_price" in kept else None
    kept = kept if kept is not None and "in_use_days" in kept else None
    async with module.session() as s:
        a = await service.assumptions(s)
        price_list = await service.prices(s)
        waiting = await service.proposals(s)

    def value(name: str, fallback: Any) -> str:
        return str(kept.get(name) or "") if kept is not None else str(fallback)

    def new(name: str, fallback: str = "") -> str:
        return str(kept_price.get(name) or "") if kept_price is not None else fallback

    rule = a.seat_rule
    return {
        "a": a,
        "v": {
            "laptop_kinds": value("laptop_kinds", _csv(a.laptop_kinds)),
            "desktop_kinds": value("desktop_kinds", _csv(a.desktop_kinds)),
            "in_use_days": value("in_use_days", a.in_use_days),
            "must_replace_age": value("must_replace_age", a.must_replace_age),
            "unsupported_os": value("unsupported_os", _csv(a.unsupported_os)),
            "upgradeable_models": value("upgradeable_models", _csv(a.upgradeable_models)),
            "equipment_models": value("equipment_models", _csv(a.equipment_models)),
            "pay_types": value("pay_types", _csv(rule.pay_types)),
            "dept_groups": value("dept_groups", _csv(rule.dept_groups)),
            "departments": value("departments", _csv(rule.departments)),
            "positions": value("positions", _csv(rule.positions)),
            "active_status": value("active_status", a.active_status),
            "growth_years": value("growth_years", a.growth_years),
            "kit_replacement_pct": value("kit_replacement_pct", f"{a.kit_replacement_pct:g}"),
            "contingency_pct": value("contingency_pct", f"{a.contingency_pct:g}"),
            "steady_years": value("steady_years", a.steady_years),
            "low_factor": value("low_factor", f"{a.low_factor * 100:g}"),
            "high_factor": value("high_factor", f"{a.high_factor * 100:g}"),
            "hr_dataset": value("hr_dataset", a.hr_dataset),
            "purchases_dataset": value("purchases_dataset", a.purchases_dataset),
            "plan": value("plan", a.plan),
            "plan_amount": value("plan_amount", f"{a.plan_amount:.0f}" if a.plan_amount else ""),
            "reuse_pct": value("reuse_pct", f"{a.reuse_pct:g}"),
            "price_rise_pct": value("price_rise_pct", f"{a.price_rise_pct:g}"),
            "lease_markup_pct": value("lease_markup_pct", f"{a.lease_markup_pct:g}"),
        },
        "plans": [(k, name) for k, (name, _) in PLANS.items()],
        "rates": [
            {
                "name": f"rate_{i}",
                "label": f"{band}: % replaced a year",
                "value": value(f"rate_{i}", f"{a.replace_rates.get(band, 0):g}"),
            }
            for i, band in enumerate(BANDS)
        ],
        "pay_type_values": await _choices(a.hr_dataset, "pay_type"),
        "dept_group_values": await _choices(a.hr_dataset, "dept_group"),
        "prices": [
            {
                "id": p.id,
                "item": p.item,
                "category": p.category,
                "unit_price": f"{p.unit_price:.2f}",
                "source": p.source,
                "quoted_on": str(p.quoted_on) if p.quoted_on else "",
            }
            for p in price_list
        ],
        "price_rows": [
            {
                "item": p.item,
                "for": PRICE_LABELS.get(p.category, p.category),
                "price": money(p.unit_price),
                "source": p.source,
                "quoted": str(p.quoted_on) if p.quoted_on else "",
                "remove": {
                    "action": f"/api/budget/prices/{p.id}/delete",
                    "label": "Remove",
                    "variant": "ghost",
                    "confirm": f"Remove {p.item} from the price list?",
                },
            }
            for p in price_list
        ],
        "new_price": {
            k: new(k, "standard_laptop" if k == "category" else "")
            for k in ("item", "category", "unit_price", "source", "quoted_on")
        },
        "categories": list(PRICE_CATEGORIES.items()),
        "proposals": [_proposal_view(x, a, {q.id: q for q in price_list}) for x in waiting],
        "can_approve": _can("budget.approve"),
        "error": request.query_params.get("error"),
    }


def _shown_value(value: Any) -> str:
    if isinstance(value, list):
        return _csv([str(v) for v in value]) or "—"  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, dict):
        parts = [f"{k}: {_shown_value(v)}" for k, v in value.items()]  # pyright: ignore[reportUnknownVariableType]
        return "; ".join(parts)
    return "—" if value in (None, "") else str(value)


def _price_fields(values: dict[str, Any]) -> dict[str, str]:
    return {
        "Item": values.get("item") or "",
        "For": PRICE_CATEGORIES.get(values.get("category") or "", ""),
        "Price": money(float(values["unit_price"])) if values.get("unit_price") else "",
        "Source": values.get("source") or "",
        "Quoted": values.get("quoted_on") or "",
    }


def _proposal_view(p: Proposal, a: Assumptions, by_id: dict[int, Price]) -> dict[str, Any]:
    data = service.proposal_data(p)
    current = by_id.get(p.price_id) if p.price_id is not None else None
    now = (
        _price_fields(
            {
                "item": current.item,
                "category": current.category,
                "unit_price": str(current.unit_price),
                "source": current.source,
                "quoted_on": str(current.quoted_on) if current.quoted_on else "",
            }
        )
        if current
        else {}
    )
    if p.kind == "assumptions":
        before = a.model_dump()
        rows = [
            {
                "field": service.ASSUMPTION_LABELS.get(k, k),
                "before": _shown_value(before.get(k)),
                "after": _shown_value(v),
            }
            for k, v in data.items()
        ]
    elif p.kind == "price_remove":
        rows = [{"field": k, "before": v, "after": "(removed)"} for k, v in now.items()]
    else:
        rows = [
            {"field": k, "before": now.get(k, ""), "after": v}
            for k, v in _price_fields(data).items()
            if p.kind == "price_add" or now.get(k, "") != v
        ]
    title = service.PROPOSAL_KINDS[p.kind] + (f": {data['item']}" if data.get("item") else "")
    return {
        "id": p.id,
        "title": title,
        "is_change": p.kind != "price_add",
        "rows": rows,
        "note": p.note,
        "by": p.proposed_by,
        "when": _when(p.proposed_at),
    }


def budget_period(forecast: dict[str, Any], saved: dt.date) -> tuple[dt.date, dt.date, str]:
    """(first day, last day, label) of what a version budgets: its plan's calendar year (ADR
    0020), or, saved before plans, the 12 months from the day it was saved."""
    if start := forecast.get("plan_start"):
        return dt.date(start, 1, 1), dt.date(start, 12, 31), str(start)
    last = saved + dt.timedelta(days=364)
    return saved, last, "its 12 months"


@module.page(
    "/v/{version_id}", template="version.html", title="Budget version", permission="budget.read"
)
async def version_page(request: Request) -> dict[str, Any]:
    try:
        version_id = int(request.path_params["version_id"])
    except ValueError:
        raise HTTPException(status_code=404, detail="Version not found") from None
    async with module.session() as s:
        v = await service.get_version(s, version_id)
        if v is None:
            raise HTTPException(status_code=404, detail="Version not found")
        snap = service.snapshot(v)
        first, last, period = budget_period(snap["forecast"], v.created_at.date())
        spent = await service.purchases_between(s, first, last)
    a = Assumptions.model_validate(snap["assumptions"])
    total = float(v.total) if v.total is not None else None
    tracking = None
    if spent is not None:
        today = dt.date.today()
        months = min(12, max(0, (min(today, last) - first).days // 30))
        tiles: list[dict[str, Any]] = [
            {"label": f"Spent in {period} (purchase history)", "value": money(spent),
             "basis": "data"}
        ]  # fmt: skip
        if total:
            tiles.append(
                {
                    "label": f"Budgeted for {period}",
                    "value": money(total),
                    "change": (
                        f"{period} hasn't started"
                        if today < first
                        else f"{spent / total * 100:.0f}% spent after {plural(months, 'month')}"
                    ),
                }
            )
        tracking = {"tiles": tiles}
    return {
        "v": v,
        **forecast_context(snap["forecast"], a),
        "status": STATUS[v.status],
        "created": _when(v.created_at),
        "adopted": _when(v.adopted_at),
        "assumption_rows": [
            {"what": "Seat rule (pay types)", "value": _csv(a.seat_rule.pay_types) or "—"},
            {
                "what": "Replacement rates",
                "value": "; ".join(f"{b}: {a.replace_rates.get(b, 0):g}%" for b in BANDS),
            },
            {
                "what": "Must replace",
                "value": f"{a.must_replace_age}+ years, or "
                f"{_csv(a.unsupported_os) or 'no OS rule'}",
            },
            {
                "what": "Upgraded in place, by model (hardware not known)",
                "value": _csv(a.upgradeable_models) or "—",
            },
            {"what": "Models left out (own equipment)", "value": _csv(a.equipment_models) or "—"},
            {"what": "Peripherals wearing out", "value": f"{a.kit_replacement_pct:g}% a year"},
            {"what": "Contingency", "value": f"{a.contingency_pct:g}%"},
            {"what": "Five-year plan", "value": PLANS.get(a.plan, PLANS["minimum"])[0]},
            {"what": "Replacement cycle", "value": f"{a.steady_years} years"},
            {
                "what": "Yearly amount (Yearly amount plan)",
                "value": money(a.plan_amount) if a.plan_amount > 0 else "Not set",
            },
            {"what": "Leavers' computers reused", "value": f"{a.reuse_pct:g}%"},
            {"what": "Prices rise", "value": f"{a.price_rise_pct:g}% a year"},
            {"what": "A lease costs more by", "value": f"{a.lease_markup_pct:g}%"},
        ],
        "price_rows": [
            {
                "item": p["item"],
                "category": PRICE_CATEGORIES.get(p["category"], p["category"]),
                "price": money(p["unit_price"]),
                "source": p.get("source") or "",
            }
            for p in snap["prices"]
        ],
        "tracking": tracking,
        "can_write": _can("budget.write"),
        "can_approve": _can("budget.approve"),
        "error": request.query_params.get("error"),
    }


@module.page(
    "/v/{version_id}/summary",
    template="summary.html",
    title="Budget summary",
    permission="budget.read",
)
async def summary_page(request: Request) -> dict[str, Any]:
    try:
        version_id = int(request.path_params["version_id"])
    except ValueError:
        raise HTTPException(status_code=404, detail="Version not found") from None
    async with module.session() as s:
        v = await _version_or_404(s, version_id)
        adopted = next(
            (x for x in await service.versions(s) if x.status == "adopted" and x.id != v.id),
            None,
        )
    return {
        "v": v,
        **summary_context(service.snapshot(v), adopted),
        "adopted": _when(v.adopted_at),
        "download": f"/api/budget/versions/{v.id}/summary",
    }


# ---- downloads -----------------------------------------------------------------------------


@module.router.get("/versions/{version_id}/summary")
async def summary_export(version_id: int, _: CanRead, format: str = "xlsx") -> Response:
    async with module.session() as s:
        v = await _version_or_404(s, version_id)
    f = service.snapshot(v)["forecast"]
    rows: list[dict[str, Any]] = [
        {
            "line": x["label"],
            "count": x["count"],
            "amount": x["amount"],
            "basis": BASIS[x["basis"]][0],
            "note": x["note"],
        }
        for x in f["lines"]
    ]
    rows.append({"line": "Total", "amount": f["total"]})
    chosen = _chosen(f)
    if chosen is not None:
        for y in chosen["years"]:
            rows.append(
                {
                    "line": f"{chosen['name']} plan: {y['year']}",
                    "count": round(y["bought"], 1),
                    "amount": y["total"],
                    "basis": "estimate",
                    "note": f"{round(y['must'])} must go, {round(y['failures'])} fail, "
                    f"{round(y['early'])} early, {round(y['reused'])} reused",
                }
            )
    for r in replacement_list(f):
        rows.append(
            {
                "line": f"{'Must replace' if r['plain']['list'] == 'Must go' else 'Replace early'}"
                f": {r['plain']['name']}",
                "count": 1,
                "basis": r["model"],
                "note": r["why"],
            }
        )
    for c in f.get("skipped", []):
        rows.append(
            {
                "line": f"Skipped to next year: {c['name']}",
                "count": 0,
                "basis": c["model"],
                "note": f"{_why(c)}. Skipped by {c['skipped_by']}: {c['skipped']}",
            }
        )
    for c in f.get("upgrades", []):
        rows.append(
            {
                "line": f"Upgrade in place: {c['name']}",
                "count": 1,
                "basis": c["model"],
                "note": c["os"],
            }
        )
    columns = [
        Column("line", "Line"),
        Column("count", "Computers or kits", "number"),
        Column("amount", "Amount", "money"),
        Column("basis", "Basis or model"),
        Column("note", "How it's worked out"),
    ]
    try:
        return export_response(rows, columns, filename=f"Budget {v.title}", format=format)
    except ExportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@module.router.get("/must-replace")
async def must_replace_export(_: CanRead, format: str = "xlsx") -> Response:
    async with module.session() as s:
        result, _a = await service.current(s)
    f = result.as_dict()
    rows = [
        {
            **r["plain"],
            "class": r["class"],
            "model": r["model"],
            "os": r["os"],
            "why": r["why"],
        }
        for r in replacement_list(f)
    ] + [
        {
            "name": c["name"],
            "list": f"Skipped to {(result.plan_start or 0) + 1}",
            "class": CLASS_NAMES.get(c["cls"], c["cls"]),
            "model": c["model"],
            "age": c["age"],
            "acquired": c["acquired"],
            "os": c["os"],
            "last_seen": c["last_seen"],
            "why": f"{_why(c)}. Skipped by {c['skipped_by']}: {c['skipped']}",
        }
        for c in f["skipped"]
    ]
    columns = [
        Column("name", "Computer"),
        Column("list", "List"),
        Column("class", "Class"),
        Column("model", "Model"),
        Column("age", "Age (years)", "number"),
        Column("acquired", "Bought (or estimated)", "date"),
        Column("os", "Operating system"),
        Column("last_seen", "Last seen", "date"),
        Column("why", "Why"),
    ]
    try:
        year = result.plan_start or dt.date.today().year
        return export_response(rows, columns, filename=f"Replacement list {year}", format=format)
    except ExportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- actions -------------------------------------------------------------------------------


def _int(text: str, label: str, low: int, high: int) -> int:
    try:
        n = int(text.strip())
    except ValueError:
        raise BudgetError(f"{label} has to be a whole number.") from None
    if not low <= n <= high:
        raise BudgetError(f"{label} has to be between {low} and {high}.")
    return n


def _pct(text: str, label: str, high: float = 100) -> float:
    try:
        n = float(text.replace("%", "").strip())
    except ValueError:
        raise BudgetError(f"{label} has to be a number.") from None
    if not 0 <= n <= high:
        raise BudgetError(f"{label} has to be between 0 and {high:g}.")
    return n


def _amount(text: str) -> float:
    text = text.replace("$", "").replace(",", "").strip()
    if not text:
        return 0.0
    try:
        n = float(text)
    except ValueError:
        raise BudgetError("The yearly amount has to be a number of dollars.") from None
    if n < 0:
        raise BudgetError("The yearly amount can't be negative.")
    return n


def _plan_levers(form: KeptForm | Any) -> dict[str, Any]:
    """The five-year plan's fields from a form (the plan form or the assumptions form)."""

    defaults = Assumptions()

    def get(name: str) -> str:
        """The field, or its default when it's left empty."""
        text = str(form.get(name) or "").strip()
        return text or ("" if name == "plan_amount" else str(getattr(defaults, name)))

    plan = get("plan")
    if plan not in PLANS:
        raise BudgetError("Choose one of the plans.")
    levers: dict[str, Any] = {
        "plan": plan,
        "steady_years": _int(get("steady_years"), "Replacement cycle (years)", 1, 15),
        "plan_amount": _amount(get("plan_amount")),
        "reuse_pct": _pct(get("reuse_pct"), "Leavers' computers reused"),
        "price_rise_pct": _pct(get("price_rise_pct"), "Prices rise a year", 50),
        "lease_markup_pct": _pct(get("lease_markup_pct"), "A lease costs more by", 200),
    }
    if plan == "amount" and not levers["plan_amount"]:
        raise BudgetError("The Yearly amount plan needs a yearly amount.")
    return levers


def _assumptions_from(form: KeptForm | Any) -> Assumptions:
    def get(name: str) -> str:
        return str(form.get(name) or "")

    return Assumptions(
        laptop_kinds=_split(get("laptop_kinds")),
        desktop_kinds=_split(get("desktop_kinds")),
        in_use_days=_int(get("in_use_days"), "Seen within (days)", 7, 730),
        replace_rates={b: _pct(get(f"rate_{i}"), f"The {b} rate") for i, b in enumerate(BANDS)},
        must_replace_age=_int(get("must_replace_age"), "Must replace at (years)", 1, 30),
        unsupported_os=_split(get("unsupported_os")),
        upgradeable_models=_split(get("upgradeable_models")),
        equipment_models=_split(get("equipment_models")),
        seat_rule=SeatRule(
            pay_types=_split(get("pay_types")),
            dept_groups=_split(get("dept_groups")),
            departments=_split(get("departments")),
            positions=_split(get("positions")),
        ),
        active_status=get("active_status").strip() or "Active",
        growth_years=_int(get("growth_years"), "Years of hiring history", 1, 10),
        kit_replacement_pct=_pct(get("kit_replacement_pct"), "Peripherals wearing out"),
        contingency_pct=_pct(get("contingency_pct"), "Contingency"),
        low_factor=_pct(get("low_factor"), "Low scenario", 500) / 100,
        high_factor=_pct(get("high_factor"), "High scenario", 500) / 100,
        hr_dataset=get("hr_dataset").strip() or "hr-employees",
        purchases_dataset=get("purchases_dataset").strip() or "device-purchases",
        **_plan_levers(form),
    )


@module.router.post("/assumptions")
async def save_assumptions(request: Request, who: CanWrite) -> RedirectResponse:
    form = await request.form()
    try:
        a = _assumptions_from(form)
        if not a.laptop_kinds and not a.desktop_kinds:
            raise BudgetError("Name at least one device kind that counts as a computer.")
        async with module.session() as s:
            await service.save_assumptions(s, who, a)
            await s.commit()
    except BudgetError as exc:
        return await module.forms.back(request, "/budget/assumptions", str(exc))
    return _back("/budget/")


@module.router.post("/plan")
async def save_plan(request: Request, who: CanWrite) -> RedirectResponse:
    """Choose the five-year plan and its levers (the must-replace age with them)."""
    form = await request.form()
    try:
        levers = _plan_levers(form)
        levers["must_replace_age"] = _int(
            str(form.get("must_replace_age") or ""), "Must replace at (years old)", 1, 30
        )
        async with module.session() as s:
            a = await service.assumptions(s)
            await service.save_assumptions(s, who, a.model_copy(update=levers))
            await s.commit()
    except BudgetError as exc:
        return await _form_back(request, "/budget/#plan", str(exc))
    return _back("/budget/#plan")


async def _price_or_404(s: Any, price_id: int) -> Price:
    p = await s.get(Price, price_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Price not found")
    return p


@module.router.post("/prices")
async def add_price(
    request: Request,
    who: CanWrite,
    item: Text = "",
    category: Text = "",
    unit_price: Text = "",
    source: Text = "",
    quoted_on: Text = "",
) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.add_price(
                s,
                who,
                item=item,
                category=category,
                unit_price=unit_price,
                source=source,
                quoted_on=quoted_on,
            )
            await s.commit()
    except BudgetError as exc:
        return await module.forms.back(request, "/budget/assumptions#prices", str(exc))
    return _back("/budget/assumptions#prices")


@module.router.post("/prices/{price_id}")
async def update_price(
    price_id: int,
    who: CanWrite,
    item: Text = "",
    category: Text = "",
    unit_price: Text = "",
    source: Text = "",
    quoted_on: Text = "",
) -> RedirectResponse:
    try:
        async with module.session() as s:
            p = await _price_or_404(s, price_id)
            await service.update_price(
                s,
                who,
                p,
                item=item,
                category=category,
                unit_price=unit_price,
                source=source,
                quoted_on=quoted_on,
            )
            await s.commit()
    except BudgetError as exc:
        return _back("/budget/assumptions#prices", str(exc))
    return _back("/budget/assumptions#prices")


@module.router.post("/prices/{price_id}/delete")
async def delete_price(price_id: int, who: CanWrite) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.delete_price(s, who, await _price_or_404(s, price_id))
            await s.commit()
    except BudgetError as exc:
        return _back("/budget/assumptions#prices", str(exc))
    return _back("/budget/assumptions#prices")


async def _proposal_or_404(s: Any, proposal_id: int) -> Proposal:
    found = await service.get_proposal(s, proposal_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return found


@module.router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: int, who: CanApprove) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.approve_proposal(s, who, await _proposal_or_404(s, proposal_id))
            await s.commit()
    except BudgetError as exc:
        return _back("/budget/proposals", str(exc))
    return _back("/budget/proposals")


@module.router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: int, who: CanApprove) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.reject_proposal(s, who, await _proposal_or_404(s, proposal_id))
            await s.commit()
    except BudgetError as exc:
        return _back("/budget/proposals", str(exc))
    return _back("/budget/proposals")


@module.router.post("/exclusions")
async def exclude(
    request: Request, who: CanWrite, name: Text = "", reason: Text = ""
) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.exclude(s, who, name, reason)
            await s.commit()
    except BudgetError as exc:
        return await _form_back(request, "/budget/#left-out", str(exc))
    return _back("/budget/#left-out")


@module.router.post("/exclusions/{exclusion_id}/delete")
async def include(exclusion_id: int, who: CanWrite) -> RedirectResponse:
    async with module.session() as s:
        x = await s.get(Exclusion, exclusion_id)
        if x is None:
            raise HTTPException(status_code=404, detail="Not left out")
        try:
            await service.include(s, who, x)
            await s.commit()
        except BudgetError as exc:
            return _back("/budget/#left-out", str(exc))
    return _back("/budget/#left-out")


@module.router.post("/skips")
async def skip(
    request: Request, who: CanWrite, asset_id: Text = "", reason: Text = ""
) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.skip(s, who, asset_id, reason)
            await s.commit()
    except BudgetError as exc:
        return await _form_back(request, "/budget/replacements#skip", str(exc))
    return _back("/budget/replacements")


@module.router.post("/skips/{skip_id}/delete")
async def unskip(skip_id: int, who: CanWrite) -> RedirectResponse:
    async with module.session() as s:
        x = await s.get(Skip, skip_id)
        if x is None:
            raise HTTPException(status_code=404, detail="Not skipped")
        try:
            await service.unskip(s, who, x)
            await s.commit()
        except BudgetError as exc:
            return _back("/budget/replacements", str(exc))
    return _back("/budget/replacements")


@module.router.post("/versions")
async def save_version(request: Request, who: CanWrite, title: Text = "") -> RedirectResponse:
    try:
        async with module.session() as s:
            v = await service.save_version(s, who, title)
            await s.commit()
            version_id = v.id
    except BudgetError as exc:
        return await _form_back(request, "/budget/#versions", str(exc))
    return _back(f"/budget/v/{version_id}")


async def _version_or_404(s: Any, version_id: int) -> Any:
    v = await service.get_version(s, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


@module.router.post("/versions/{version_id}/adopt")
async def adopt_version(version_id: int, who: CanApprove) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.adopt(s, who, await _version_or_404(s, version_id))
            await s.commit()
    except BudgetError as exc:
        return _back(f"/budget/v/{version_id}", str(exc))
    return _back(f"/budget/v/{version_id}")


@module.router.post("/versions/{version_id}/delete")
async def delete_version(version_id: int, who: CanWrite) -> RedirectResponse:
    try:
        async with module.session() as s:
            await service.delete_version(s, who, await _version_or_404(s, version_id))
            await s.commit()
    except BudgetError as exc:
        return _back(f"/budget/v/{version_id}", str(exc))
    return _back("/budget/")
