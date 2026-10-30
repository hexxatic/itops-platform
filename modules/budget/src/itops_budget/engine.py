"""The device forecast (ADR 0016): plain arithmetic on the fleet, HR history, purchase
history, the price list and editable assumptions. Never AI.

Everything here is pure (no database, no services) so it can be tested with plain values
and frozen into a saved version. Missing data never stops a forecast: a line falls back to
an assumption, says so in its basis, and the gap is listed under "what would firm it up".
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

from pydantic import BaseModel, Field, field_validator

BANDS = ("Under 3 years", "3 to 5 years", "5 to 7 years", "7 years or more", "Unknown age")
# Bands were mislabelled before 2026-10-19 (the 3-to-5 band read "3 to 4 years"); saved
# assumptions and versions still carry the old labels.
RENAMED_BANDS = {"3 to 4 years": BANDS[1], "5 to 6 years": BANDS[2]}
# Computer classes (Assets' Computer classes): each is priced and replaced as itself.
CLASSES = {
    "standard_laptop": "Standard laptops",
    "standard_desktop": "Standard desktops",
    "engineering_laptop": "Engineering laptops",
    "engineering_desktop": "Engineering desktops",
}
# Versions saved before classes counted laptops and desktops.
LEGACY_CLASSES = {"laptop": "Laptops", "desktop": "Desktops"}
CLASS_LABELS = CLASSES | LEGACY_CLASSES
CLASS_NAMES = {k: v[:-1] for k, v in CLASS_LABELS.items()}  # "Standard laptop"
PRICE_CATEGORIES = {
    "standard_laptop": "Standard laptop (per computer)",
    "standard_desktop": "Standard desktop (per computer)",
    "engineering_laptop": "Engineering laptop (per computer)",
    "engineering_desktop": "Engineering desktop (per computer)",
    "kit": "Kit item (per new seat: dock, monitor, keyboard, ...)",
}
# Price-list entries from before classes: a laptop price is the standard laptop's.
PRICE_ALIASES = {"laptop": "standard_laptop", "desktop": "standard_desktop"}
PRICE_LABELS = PRICE_CATEGORIES | {k: PRICE_CATEGORIES[v] for k, v in PRICE_ALIASES.items()}
# Statuses that take a computer out of the fleet being forecast. Spare is counted apart.
OUT = {"Retired", "Inactive", "Ignored"}
# The HR dataset's columns, by key (an HR connector writes these; ADR 0016).
HR = {
    "status": "status",
    "pay_type": "pay_type",
    "dept_group": "dept_group",
    "department": "department",
    "position": "position",
    "hired": "hire_date",
    "rehired": "rehire_date",
    "terminated": "termination_date",
    "previously_terminated": "previous_termination_date",
}
# The purchase-history dataset's columns, by key (Reports' starter type *purchase*).
PURCHASE = {
    "date": "date",
    "category": "category",
    "quantity": "quantity",
    "total": "total",
    "unit_cost": "unit_cost",
    "reason": "reason",
}
COMPUTER_CATEGORIES = {"laptop", "desktop", "computer", "workstation", *CLASSES}


class SeatRule(BaseModel):
    """Who gets a computer of their own: anyone matching one of these."""

    pay_types: list[str] = Field(default_factory=lambda: ["Salary"])
    dept_groups: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    positions: list[str] = Field(default_factory=list)


class Assumptions(BaseModel):
    """What the forecast assumes until data says otherwise. Percentages are 0-100."""

    laptop_kinds: list[str] = Field(default_factory=lambda: ["Laptop"])
    desktop_kinds: list[str] = Field(default_factory=lambda: ["Workstation"])
    in_use_days: int = 90
    replace_rates: dict[str, float] = Field(
        default_factory=lambda: dict(zip(BANDS, (2.0, 5.0, 15.0, 30.0, 10.0), strict=True))
    )
    must_replace_age: int = 8
    unsupported_os: list[str] = Field(
        default_factory=lambda: ["Windows 7", "Windows 8", "Windows 10"]
    )
    # Models whose hardware runs a supported system: on an unsupported one they're upgraded
    # in place, not replaced (ADR 0018). Only for computers Assets can't judge by their
    # hardware (OS readiness, ADR 0019): readiness wins when it's known.
    upgradeable_models: list[str] = Field(default_factory=list)
    # Models of PCs that are their own equipment (machine controllers, gauges, room
    # systems): not bought from this budget, so left out and listed.
    equipment_models: list[str] = Field(default_factory=list)
    seat_rule: SeatRule = Field(default_factory=SeatRule)
    active_status: str = "Active"
    growth_years: int = 3
    kit_replacement_pct: float = 5.0
    contingency_pct: float = 10.0
    steady_years: int = 5  # the replacement cycle, in years (cycle and lease plans)
    low_factor: float = 0.75
    high_factor: float = 1.25
    hr_dataset: str = "hr-employees"
    purchases_dataset: str = "device-purchases"
    # The five-year plan (ADR 0020): which one budgets next year, and its levers.
    plan: str = "minimum"  # minimum | even | cycle | amount | lease (plan.PLANS)
    plan_amount: float = 0.0  # a year, for the amount plan; 0 = not set
    reuse_pct: float = 50.0  # share of net leavers' computers given to someone else
    price_rise_pct: float = 3.0  # a year, after year 1
    lease_markup_pct: float = 15.0  # a lease's cost over the purchase price, over the term

    @field_validator("replace_rates")
    @classmethod
    def _renamed(cls, rates: dict[str, float]) -> dict[str, float]:
        return {RENAMED_BANDS.get(k, k): v for k, v in rates.items()}


@dataclass
class Price:
    item: str
    category: str  # a class (standard_laptop, ...) | kit (older entries: laptop | desktop)
    unit_price: float
    source: str = ""
    quoted_on: str | None = None


@dataclass
class Line:
    key: str
    label: str
    count: float | None
    amount: float | None
    basis: str  # data | partial | estimate
    note: str
    shown: str = ""  # instead of the amount, e.g. "Leased"


@dataclass
class Computer:
    id: str
    name: str
    url: str
    cls: str  # standard_laptop | standard_desktop | engineering_laptop | engineering_desktop
    band: str  # by its age today
    age: float | None  # today
    status: str
    model: str
    os: str
    last_seen: str | None
    warranty_expires: str | None
    acquired: str | None
    reasons: list[str] = field(default_factory=list[str])
    age_basis: str = ""  # purchased | warranty | in_service, serial, joined (estimates)
    # Why it counts as in use, as Assets judged it: "network" (a scan or Sophos saw it), "ad"
    # (only its AD sign-in; the device itself is trusted over AD) or "" (marked Active,
    # nothing saw it).
    seen_by: str = ""
    classified: bool = True  # False: Assets has no class for it, so it's counted as standard
    disagrees: bool = False  # Assets lists it under "Sources disagree"
    upgrade: bool = False  # on an unsupported system, but the hardware can be upgraded
    judged: str = ""  # how upgrade or not was decided: "hardware" (OS readiness) | "model"
    left_out: str = ""  # why it isn't in this budget (a model rule or a person's reason)
    left_out_by: str = ""  # who left it out, for a person's exclusion
    # Its age on 1 January of the plan's first year (the budget year): what that year's
    # must-replace and failure rates go by. Later plan years add a year each.
    age_then: float | None = None
    # Why the chosen plan replaces it early this year (empty when it doesn't).
    early: str = ""
    # A person skipped it to next year, with why: kept this year, due again the next.
    skipped: str = ""
    skipped_by: str = ""
    skipped_due: str = ""  # why it was on the list when it was skipped
    skipped_before: str = ""  # "Skipped from 2026: ...", when an earlier year skipped it


@dataclass
class Forecast:
    today: str
    lines: list[Line]
    total: float | None
    quarters: list[float]
    scenarios: dict[str, float | None]
    computers_in_use: int
    by_class: dict[str, int]
    bands: dict[str, dict[str, int]]  # class -> band -> count
    spares: int
    not_seen: int
    unreviewed: int
    must_replace: list[Computer]
    growth: list[dict[str, Any]]  # [{year, hires, terms, net}]
    net_seats: dict[str, float]  # low / expected / high
    predicted_seats: int | None
    steady: dict[str, Any]
    actuals: list[dict[str, Any]]  # [{year, computers, peripherals, other}]
    replacements_last_year: int | None
    gaps: list[str]
    missing_prices: list[str]
    upgrades: list[Computer] = field(default_factory=list[Computer])
    left_out: list[Computer] = field(default_factory=list[Computer])
    # The replacement list (must_replace, then these): computers the plan replaces early,
    # by name, and the ones a person skipped to next year.
    early: list[Computer] = field(default_factory=list[Computer])
    skipped: list[Computer] = field(default_factory=list[Computer])
    plan_start: int | None = None  # the year the plan budgets first (ADR 0020)
    plan: str = ""  # the chosen plan's key
    plans: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    # A gap's text -> where to firm it up ({"href", "text"}): the page linked after it.
    gap_links: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- helpers ------------------------------------------------------------------------------


def as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def as_number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _lower(values: list[str]) -> set[str]:
    return {v.strip().lower() for v in values if v.strip()}


def band_of(age: float | None) -> str:
    if age is None:
        return BANDS[4]
    if age < 3:
        return BANDS[0]
    if age < 5:
        return BANDS[1]
    if age < 7:
        return BANDS[2]
    return BANDS[3]


def plural(n: float, one: str, many: str | None = None) -> str:
    whole = round(n)
    return f"{whole:,} {one if whole == 1 else (many or one + 's')}"


def _price(prices: list[Price], category: str) -> float | None:
    found = [
        p.unit_price
        for p in prices
        if p.category == category or PRICE_ALIASES.get(p.category) == category
    ]
    return round(mean(found), 2) if found else None


def class_key(asset_class: Any, form: str) -> tuple[str, bool]:
    """Assets' class ("Engineering Laptop") -> (budget class key, whether Assets had one);
    without one, the standard class of the kind's form (laptop | desktop)."""
    key = str(asset_class or "").strip().lower().replace(" ", "_")
    return (key, True) if key in CLASSES else (f"standard_{form}", False)


def kit_price(prices: list[Price]) -> float | None:
    found = [p.unit_price for p in prices if p.category == "kit"]
    return round(sum(found), 2) if found else None


def has_seat(person: dict[str, Any], rule: SeatRule) -> bool:
    def matches(column: str, wanted: list[str]) -> bool:
        return str(person.get(HR[column]) or "").strip().lower() in _lower(wanted)

    return (
        matches("pay_type", rule.pay_types)
        or matches("dept_group", rule.dept_groups)
        or matches("department", rule.departments)
        or matches("position", rule.positions)
    )


def _quarters(total: float | None) -> list[float]:
    """A total spread evenly over four quarters, the last taking the rounding."""
    if total is None:
        return []
    q = round(total / 4, 2)
    return [q, q, q, round(total - 3 * q, 2)]


# ---- the parts ----------------------------------------------------------------------------


def _judge(
    c: Computer,
    os_ready: list[dict[str, Any]],
    unsupported: list[re.Pattern[str]],
    upgradeable: set[str],
) -> None:
    """A computer on an unsupported system: upgraded in place if its hardware is ready for
    a supported one (Assets' OS readiness), replaced if it's ready for none, and judged by
    model (``upgradeable_models``) when its hardware isn't known."""
    targets = [
        r for r in os_ready if not any(p.search(str(r.get("target") or "")) for p in unsupported)
    ]
    if any(r.get("status") == "ready" for r in targets):
        c.upgrade, c.judged = True, "hardware"
    elif targets and all(r.get("status") == "not_ready" for r in targets):
        r = targets[0]
        whys: list[Any] = r.get("why") or []
        why = "; ".join(str(w) for w in whys) or "hardware"
        c.reasons.append(f"runs {c.os}; can't run {r['target']}: {why}")
        c.judged = "hardware"
    elif c.model.strip().lower() in upgradeable:
        c.upgrade, c.judged = True, "model"
    else:
        # Nothing checked its hardware: say so, so the list doesn't read as if it was.
        model = f"model {c.model} isn't on the upgradeable list" if c.model else "no model recorded"
        c.reasons.append(f"runs {c.os}; hardware not checked, and {model}")
        c.judged = "model"


def computers(
    fleet: list[dict[str, Any]],
    a: Assumptions,
    today: dt.date,
    excluded: dict[str, tuple[str, str]] | None = None,
    as_of: dt.date | None = None,
    skips: dict[str, list[tuple[int, str, str, str]]] | None = None,
) -> tuple[list[Computer], int, int, int, list[Computer]]:
    """The computers in use, how many are spare, not seen lately and unreviewed, and the
    in-use computers left out of the budget: a model on `equipment_models`, or excluded by a
    person (``excluded``: asset id -> (reason, who)). Too old means by ``as_of`` (the budget
    year's 1 January; today when not given). ``skips``: asset id -> [(budget year, reason,
    who, why it was due)], replacements a person skipped; the one for ``as_of``'s year
    holds it this year."""
    excluded = excluded or {}
    skips = skips or {}
    as_of = as_of or today
    forms = {k.lower(): "laptop" for k in a.laptop_kinds} | {
        k.lower(): "desktop" for k in a.desktop_kinds
    }
    unsupported = [
        re.compile(re.escape(p.strip()) + r"\b", re.I) for p in a.unsupported_os if p.strip()
    ]
    upgradeable = _lower(a.upgradeable_models)
    equipment = _lower(a.equipment_models)
    seen_since = today - dt.timedelta(days=a.in_use_days)
    out: list[Computer] = []
    left_out: list[Computer] = []
    spares = not_seen = unreviewed = 0
    for d in fleet:
        form = forms.get(str(d.get("kind") or "").lower())
        status = str(d.get("status") or "")
        if form is None or status in OUT:
            continue
        if status == "Spare":
            spares += 1
            continue
        # When the device itself was last seen (a scan or Sophos, Assets' ``seen_on``).
        last_seen = as_date(d.get("seen_on") or d.get("last_seen"))
        # Assets judges who saw it (the device itself over AD, within the window Budget passes
        # it); without its verdict, a scan within the window.
        seen_by = d.get("seen_by")
        if seen_by is None:
            seen_by = "network" if last_seen is not None and last_seen >= seen_since else ""
        if status != "Active" and not seen_by:
            not_seen += 1
            continue
        if status == "Unreviewed":
            unreviewed += 1
        acquired = as_date(d.get("acquired"))
        age = round((today - acquired).days / 365.25, 1) if acquired else None
        age_then = round((as_of - acquired).days / 365.25, 1) if acquired else None
        cls, classified = class_key(d.get("computer_class"), form)
        c = Computer(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            url=str(d.get("url") or ""),
            cls=cls,
            band=band_of(age),
            age=age,
            status=status or "—",
            model=str(d.get("model") or ""),
            os=str(d.get("operating_system") or ""),
            last_seen=str(last_seen) if last_seen else None,
            warranty_expires=d.get("warranty_expires"),
            acquired=d.get("acquired"),
            age_basis=str(d.get("age_basis") or "") if acquired else "",
            seen_by=str(seen_by),
            classified=classified,
            disagrees=bool(d.get("disagreements")),
            age_then=age_then,
        )
        if c.id in excluded:
            c.left_out, c.left_out_by = excluded[c.id]
            left_out.append(c)
            continue
        if c.model.strip().lower() in equipment:
            c.left_out = f"Its own equipment (model {c.model})"
            left_out.append(c)
            continue
        if age_then is not None and age_then >= a.must_replace_age:
            when = "" if as_of == today else f" on 1 January {as_of.year}"
            c.reasons.append(f"{age_then:g} years old{when}")
        if c.os and any(p.search(c.os) for p in unsupported):
            _judge(c, d.get("os_ready") or [], unsupported, upgradeable)
        for year, reason, who, due in sorted(skips.get(c.id, [])):
            if year == as_of.year:
                c.skipped, c.skipped_by, c.skipped_due = reason, who, due
            elif year < as_of.year:
                c.skipped_before = f"Skipped from {year}: {reason}"
        out.append(c)
    return out, spares, not_seen, unreviewed, left_out


def growth_history(
    people: list[dict[str, Any]], rule: SeatRule, years: int, today: dt.date
) -> list[dict[str, Any]]:
    """Hires and leavers with a seat, per full year (the current year is left out)."""
    span = list(range(today.year - years, today.year))
    hires = dict.fromkeys(span, 0)
    terms = dict.fromkeys(span, 0)
    for p in people:
        if not has_seat(p, rule):
            continue
        for column, counts in (
            ("hired", hires),
            ("rehired", hires),
            ("terminated", terms),
            ("previously_terminated", terms),
        ):
            d = as_date(p.get(HR[column]))
            if d and d.year in counts:
                counts[d.year] += 1
    return [
        {"year": y, "hires": hires[y], "terms": terms[y], "net": hires[y] - terms[y]} for y in span
    ]


def purchase_date(row: dict[str, Any]) -> dt.date | None:
    return as_date(row.get(PURCHASE["date"])) or as_date(row.get("_time"))


def spent(row: dict[str, Any]) -> float | None:
    """A purchase's total, else quantity times unit cost (None without money columns)."""
    total = as_number(row.get(PURCHASE["total"]))
    if total is None:
        unit = as_number(row.get(PURCHASE["unit_cost"]))
        if unit is not None:
            total = unit * (as_number(row.get(PURCHASE["quantity"])) or 1)
    return total


def actual_spend(purchases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Purchase history by year: computers, kit and peripherals, anything else."""
    years: dict[int, dict[str, float]] = {}
    for row in purchases:
        d = purchase_date(row)
        total = spent(row)
        if d is None or total is None:
            continue
        category = str(row.get(PURCHASE["category"]) or "").strip().lower()
        part = (
            "computers"
            if category in COMPUTER_CATEGORIES
            else ("other" if category in {"", "other"} else "peripherals")
        )
        y = years.setdefault(d.year, {"computers": 0.0, "peripherals": 0.0, "other": 0.0})
        y[part] += total
    return [{"year": y, **{k: round(v, 2) for k, v in years[y].items()}} for y in sorted(years)]


def replacements_bought(purchases: list[dict[str, Any]], today: dt.date) -> int | None:
    """Computers bought as replacements in the last 12 months (None without history)."""
    if not purchases:
        return None
    since = today - dt.timedelta(days=365)
    n = 0
    for row in purchases:
        d = purchase_date(row)
        category = str(row.get(PURCHASE["category"]) or "").strip().lower()
        reason = str(row.get(PURCHASE["reason"]) or "").strip().lower()
        if d and d >= since and category in COMPUTER_CATEGORIES and reason == "replacement":
            n += int(as_number(row.get(PURCHASE["quantity"])) or 1)
    return n


# ---- the forecast -------------------------------------------------------------------------


def forecast(
    *,
    fleet: list[dict[str, Any]] | None,
    people: list[dict[str, Any]] | None,
    purchases: list[dict[str, Any]] | None,
    prices: list[Price],
    assumptions: Assumptions,
    today: dt.date,
    excluded: dict[str, tuple[str, str]] | None = None,
    skips: dict[str, list[tuple[int, str, str, str]]] | None = None,
) -> Forecast:
    """The next 12 months. ``None`` for an input means it isn't available at all.
    ``excluded`` maps an asset id to (reason, who) for computers a person left out, ``skips``
    to [(budget year, reason, who, why it was due)] for replacements a person skipped to the
    next year."""
    from itops_budget import plan as planning

    a = assumptions
    gaps: list[str] = []
    # The budget is the plan's first calendar year (ADR 0020): ages as of its 1 January.
    start = planning.start_year(today.year, today.month)
    in_use, spares, not_seen, unreviewed, left_out = computers(
        fleet or [], a, today, excluded, as_of=dt.date(start, 1, 1), skips=skips
    )
    if fleet is None:
        gaps.append("Assets isn't available, so there's no fleet to forecast from.")
    by_class = {c: sum(1 for x in in_use if x.cls == c) for c in CLASSES}
    bands = {
        c: {b: sum(1 for x in in_use if x.cls == c and x.band == b) for b in BANDS} for c in CLASSES
    }
    price = {c: _price(prices, c) for c in CLASSES}
    # No engineering price yet: priced as the standard computer of the same form, and said so.
    as_standard = [c for c in CLASSES if c.startswith("engineering_") and price[c] is None]
    for c in as_standard:
        price[c] = price[c.replace("engineering_", "standard_")]
    kit = kit_price(prices)
    # Engineering falls back to standard: only a missing standard price stops the total.
    missing = [
        PRICE_CATEGORIES[c].split(" (")[0]
        for c in CLASSES
        if price[c] is None and c.startswith("standard_")
    ]
    if kit is None:
        missing.append("Kit for a new seat")
    blended = None
    if all(price[c] is not None for c in CLASSES):
        total_in_use = sum(by_class.values())
        blended = round(
            sum(by_class[c] * (price[c] or 0) for c in CLASSES) / total_in_use
            if total_in_use
            else mean(price[c] or 0 for c in CLASSES),
            2,
        )

    def cost(counts: dict[str, float]) -> float | None:
        if any(n and price[c] is None for c, n in counts.items()):
            return None
        return round(sum(n * (price[c] or 0) for c, n in counts.items()), 2)

    # Must replace: too old by the budget year's 1 January, or an operating system that's out
    # of support on hardware that can't be upgraded. Upgradeable ones are upgraded in place
    # (ADR 0018).
    # A computer a person skipped is kept this year (no purchase, not counted to fail) and is
    # due again next year.
    skipped = [c for c in in_use if c.skipped]
    must = [c for c in in_use if c.reasons and not c.skipped]
    must_counts = {c: float(sum(1 for x in must if x.cls == c)) for c in CLASSES}
    systems = ", ".join(a.unsupported_os) or "an unsupported system"
    must_line = Line(
        "must_replace",
        "Must replace",
        len(must),
        cost(must_counts),
        "partial",
        f"Computers in use that are {a.must_replace_age}+ years old by 1 January {start} or "
        f"run {systems}"
        + (" on a model that can't be upgraded" if a.upgradeable_models else "")
        + ": counted from Assets, priced from the price list."
        + (
            f" {plural(len(skipped), 'computer')} skipped to {start + 1} (Replacement list)."
            if skipped
            else ""
        ),
    )
    upgrades = [c for c in in_use if c.upgrade and not c.reasons]
    by_model = [c for c in upgrades if c.judged == "model"]
    upgrade_line = Line(
        "upgrade",
        "Upgrade in place",
        len(upgrades),
        0.0,
        "estimate" if len(by_model) == len(upgrades) else "partial" if by_model else "data",
        f"Computers that run {systems} on hardware that can take a supported system: upgraded "
        "by IT, no purchase. Judged by their processor, memory, storage and TPM (OS "
        "readiness in Assets)"
        + (
            f"; {plural(len(by_model), 'computer')} without that data by model (the "
            "upgradeable models assumption)."
            if by_model
            else "."
        ),
    )

    # Replacements as needed: a rate per age band (in the budget year), for the rest.
    rest = [c for c in in_use if not c.reasons and not c.skipped]
    rate = {b: max(0.0, a.replace_rates.get(b, 0.0)) / 100 for b in BANDS}
    repl_counts = {c: sum(rate[band_of(x.age_then)] for x in rest if x.cls == c) for c in CLASSES}
    bought = replacements_bought(purchases or [], today)
    repl_note = "An assumed share of computers per age band fails or can't keep up each year."
    if bought is not None:
        repl_note += (
            f" Purchase history: {plural(bought, 'replacement')} bought in the last 12 months."
        )
    repl_line = Line(
        "replacements",
        "Replacements as needed",
        round(sum(repl_counts.values()), 2),
        cost(repl_counts),
        "estimate",
        repl_note,
    )

    # Growth: net new seats from hiring history, beyond the spares on the shelf.
    history: list[dict[str, Any]] = []
    predicted: int | None = None
    nets = {"low": 0.0, "expected": 0.0, "high": 0.0}
    if people is None:
        gaps.append(
            "No HR headcount dataset, so growth assumes no new seats. A headcount "
            "connector syncs one into Reports."
        )
    else:
        history = growth_history(people, a.seat_rule, max(1, a.growth_years), today)
        values = [h["net"] for h in history]
        nets = {
            "low": float(min(values)),
            "expected": round(mean(values), 1),
            "high": float(max(values)),
        }
        predicted = sum(
            1
            for p in people
            if str(p.get(HR["status"]) or "").lower() == a.active_status.lower()
            and has_seat(p, a.seat_rule)
        )

    def growth_cost(net: float) -> tuple[float, float | None]:
        buy = max(0.0, net - spares)
        if buy == 0:
            return 0.0, 0.0
        if blended is None or kit is None:
            return buy, None
        return buy, round(buy * (blended + kit), 2)

    g_count, g_amount = growth_cost(nets["expected"])
    growth_line = Line(
        "growth",
        "New seats",
        g_count,
        g_amount,
        "partial" if people is not None else "estimate",
        f"Net seats a year (hires minus leavers who get a computer of their own, average of the "
        f"last {a.growth_years} full years: {nets['expected']:g}), less {plural(spares, 'spare')} "
        "on the shelf, each with a computer and a kit.",
    )

    # Peripherals wearing out across the seats already there.
    seats_now = len(in_use)
    worn = seats_now * max(0.0, a.kit_replacement_pct) / 100
    periph_line = Line(
        "peripherals",
        "Peripherals wearing out",
        round(worn, 2),
        round(worn * kit, 2) if kit is not None else None,
        "estimate",
        f"{a.kit_replacement_pct:g}% of a kit per computer in use each year.",
    )

    # The five-year plan (ADR 0020): its first year is this budget. Needs every price.
    chosen: planning.Plan | None = None
    early: list[Computer] = []
    found: dict[str, planning.Plan] = {}
    plan_lines: list[Line] = []
    if all(price[c] is not None for c in CLASSES) and kit is not None:
        found = planning.plans(
            in_use,
            {c: price[c] or 0.0 for c in CLASSES},
            a,
            start,
            seats=g_amount or 0.0,
            peripherals=periph_line.amount or 0.0,
            net_leavers=-nets["expected"],
        )
        chosen = found.get(a.plan) or found["minimum"]
        y1 = chosen.years[0]
        # By name: the oldest of each class, as many as the plan replaces early (it counts
        # by class and whole years old, oldest first).
        for cls, n in y1.early_by_class.items():
            pool = sorted(
                (c for c in rest if c.cls == cls),
                key=lambda c: (c.age_then is None, -(c.age_then or 0), c.name.lower()),
            )
            for c in pool[: round(n)]:
                old = (
                    f"{c.age_then:g} years old on 1 January {start}"
                    if c.age_then is not None
                    else "age unknown"
                )
                c.early = (
                    f"Replaced early ({chosen.name} plan): among the oldest "
                    f"{CLASS_LABELS[cls].lower()}, {old}"
                )
                early.append(c)
        lease = chosen.key == "lease"
        if lease:
            for x in (must_line, repl_line):
                x.amount, x.shown = 0.0, "Leased"
        if y1.early:
            plan_lines.append(
                Line(
                    "early",
                    f"Replaced early ({chosen.name} plan)",
                    y1.early,
                    0.0 if lease else y1.early_cost,
                    "estimate",
                    "The oldest computers, replaced before they fail so the plan's years stay "
                    "level (Five-year plan).",
                    shown="Leased" if lease else "",
                )
            )
        if y1.reused:
            plan_lines.append(
                Line(
                    "reuse",
                    "Reused from people who leave",
                    -y1.reused,
                    0.0 if lease else -y1.reuse_saving,
                    "estimate",
                    f"{a.reuse_pct:g}% of the net leavers a year ({-nets['expected']:g}): their "
                    "computers go to someone whose computer is due, instead of buying one.",
                    shown="Fewer leased" if lease else "",
                )
            )
        if lease:
            plan_lines.append(
                Line(
                    "lease",
                    "Lease payments",
                    y1.leased,
                    y1.lease,
                    "estimate",
                    f"Computers on lease, each its purchase price over {a.steady_years} years "
                    f"plus {a.lease_markup_pct:g}% (until a lease is quoted).",
                )
            )

    base = [
        must_line,
        *([upgrade_line] if upgrades else []),
        repl_line,
        *plan_lines,
        growth_line,
        periph_line,
    ]
    subtotal = None if any(x.amount is None for x in base) else sum(x.amount or 0 for x in base)
    pct = max(0.0, a.contingency_pct) / 100
    cont_line = Line(
        "contingency",
        "Contingency",
        None,
        round(subtotal * pct, 2) if subtotal is not None else None,
        "estimate",
        f"{a.contingency_pct:g}% on top, for what nobody saw coming.",
    )
    lines = [*base, cont_line]
    total = round((subtotal or 0) * (1 + pct), 2) if subtotal is not None else None

    if chosen is not None and total is not None:  # year 1 is exactly this budget
        chosen.years[0].total = total
        chosen.years[0].contingency = cont_line.amount or 0.0

    def scenario(factor: float, net: float) -> float | None:
        """This budget with more or fewer failures and the slowest or fastest growth."""
        r = cost(repl_counts)
        _, g = growth_cost(net)
        if total is None or r is None or g is None or g_amount is None:
            return None
        return round(total + ((factor - 1) * r + g - g_amount) * (1 + pct), 2)

    scenarios = {
        "low": scenario(a.low_factor, nets["low"]),
        "expected": total,
        "high": scenario(a.high_factor, nets["high"]),
    }

    # A steady policy: replace every computer at N years, the same amount every year.
    years = max(1, a.steady_years)
    turning: list[dict[str, Any]] = []
    for offset in range(6):
        y = today.year + offset
        n = 0
        for c in in_use:
            acquired = as_date(c.acquired)
            if acquired is None:
                continue
            due = acquired.year + years
            n += 1 if (due == y or (offset == 0 and due < y)) else 0
        turning.append({"year": y, "count": n})
    steady = {
        "years": years,
        "per_year": round(seats_now / years, 1),
        "yearly": round(seats_now / years * blended, 2) if blended is not None else None,
        "as_needed": (
            None
            if must_line.amount is None or repl_line.amount is None
            else round(must_line.amount + repl_line.amount, 2)
        ),
        "turning": turning,
        "unknown_age": sum(1 for c in in_use if c.acquired is None),
    }

    # What would firm it up: what's missing, what it does to the figures, and what to do
    # (AGENTS.md, writing warnings), with the page to do it on in ``links``.
    links: dict[str, dict[str, str]] = {}

    def gap(text: str, href: str = "", label: str = "", *, first: bool = False) -> None:
        if first:
            gaps.insert(0, text)
        else:
            gaps.append(text)
        if href:
            links[text] = {"href": href, "text": label}

    devices = "/assets/t/device"
    if fleet is not None and not in_use:
        if not_seen:
            gap(f"No computers count as in use: {plural(not_seen, 'computer')} "
                f"{'hasn' if not_seen == 1 else 'haven'}'t been seen on the network in "
                f"{a.in_use_days} days and none is marked Active, so there's nothing to "
                "budget. Run an inventory sync, or mark the ones in use Active.",
                "/connectors", "Connectors", first=True)  # fmt: skip
        else:
            gap("No computers count as in use: Assets has no devices of the computer kinds "
                f"({', '.join([*a.laptop_kinds, *a.desktop_kinds])}), so there's nothing to "
                "budget. Set the kind on your computers in Assets, or change the kinds counted "
                "under Assumptions.", "/budget/assumptions", "Assumptions", first=True)  # fmt: skip
    by_model = [c for c in in_use if c.judged == "model"]
    if by_model:
        models = sorted({c.model for c in by_model if c.model})
        gap(f"Whether {plural(len(by_model), 'computer')} on an unsupported system can be "
            f"upgraded is judged by model ({', '.join(models[:8])}"
            f"{', ...' if len(models) > 8 else ''}), so some may be replaced that could be "
            "upgraded, or the other way round. Assets needs their processor, memory and TPM "
            "(an inventory sync with hardware) and an OS target to check them against.",
            "/assets/readiness", "OS readiness")  # fmt: skip
    unknown = sum(1 for c in in_use if c.age is None)
    if unknown:
        gap(f"{plural(unknown, 'computer')} in use {'has' if unknown == 1 else 'have'} "
            "no purchase or warranty date, so their age is a guess and their replacement year "
            "may be off. Add a purchase date or an In service since estimate on the device.",
            devices + "?sort=age", "Devices")  # fmt: skip
    for c in as_standard:
        if by_class[c]:
            name = CLASS_NAMES[c].lower()
            gap(f"The price list has no {name} price, so {plural(by_class[c], name)} "
                f"{'is' if by_class[c] == 1 else 'are'} priced as standard and the total is "
                f"probably low. Add a {name} price.", "/budget/assumptions#prices",
                "Assumptions and prices")  # fmt: skip
    unclassified = [c for c in in_use if not c.classified]
    if unclassified:
        gap(f"{plural(len(unclassified), 'computer')} in use "
            f"{'has' if len(unclassified) == 1 else 'have'} no class in Assets (no model, "
            "or a model on neither list), so they're counted as standard. Add their models "
            "to a list on Computer classes, or set the class on the device.",
            "/assets/classes", "Computer classes")  # fmt: skip
    from_serial = sum(1 for c in in_use if c.age_basis == "serial")
    if from_serial:
        gap(f"The age of {plural(from_serial, 'computer')} is estimated from the build week "
            "in the HP serial number, usually within a few months. A purchase or warranty date "
            "(HP's warranty lookup, or entered on the device) replaces the estimate.")  # fmt: skip
    joined = sum(1 for c in in_use if c.age_basis == "joined")
    if joined:
        gap(f"The age of {plural(joined, 'computer')} comes from when it joined the domain, "
            "a rough estimate (a reimage that rejoins makes it look newer). Add a purchase "
            "date or an In service since estimate on the device.", devices + "?sort=age",
            "Devices")  # fmt: skip
    by_person = sum(1 for c in in_use if c.age_basis == "in_service")
    if by_person:
        gap(f'The age of {plural(by_person, "computer")} comes from an estimated "In service '
            'since" date in Assets. A purchase or warranty date replaces it when one is '
            "known.")  # fmt: skip
    if missing:
        gap("The price list has no "
            + ", ".join(m.lower() for m in missing)
            + " price, so the total can't be worked out yet. Add the price.",
            "/budget/assumptions#prices", "Assumptions and prices")  # fmt: skip
    if purchases is None:
        gap("No purchase history yet, so replacement rates are assumptions and past spend "
            "can't be shown. It comes from a purchases dataset in Reports, synced by a "
            "connector; until there is one, check the rates under Assumptions.",
            "/budget/assumptions", "Assumptions")  # fmt: skip
    if not_seen:
        gap(f"{plural(not_seen, 'computer')} not seen by a scan or Sophos, or signed in to AD, in "
            f"{a.in_use_days} days {'is' if not_seen == 1 else 'are'} left out of the budget. "
            "Mark each Retired if it's gone, or Active if it's in use (Active ones are "
            "counted).", devices + "?view=unseen", "Devices not seen anywhere")  # fmt: skip
    # Where the sources don't line up: Assets lists them (a scan is trusted over AD).
    only_ad = sum(1 for c in in_use if c.seen_by == "ad")
    disagree = sum(1 for c in in_use if c.disagrees)
    if disagree:
        gap(f"Sources disagree on {plural(disagree, 'computer')} counted here"
            + (f", {only_ad} of them seen only by AD" if only_ad else "")
            + ", so the count in use may be off. Sources disagree lists each one with what to "
            "do.", "/assets/disagreements", "Sources disagree")  # fmt: skip
    if unreviewed:
        gap(f"{plural(unreviewed, 'computer')} found by a sync "
            f"{'is' if unreviewed == 1 else 'are'} still Unreviewed in Assets: counted if "
            "seen in use, but nobody has checked what they are. Set each one's status and "
            "kind.", devices + "?view=unreviewed", "Unreviewed devices")  # fmt: skip
    if predicted is not None and seats_now and abs(predicted - seats_now) > 0.2 * seats_now:
        gap(f"The seat rule predicts {predicted:,} people with a computer of their own; "
            f"Assets counts {seats_now:,} computers in use. If the difference is shared and "
            "production computers (which hiring doesn't change), nothing to do; if not, "
            "adjust the seat rule.", "/budget/assumptions", "Assumptions")  # fmt: skip

    return Forecast(
        today=str(today),
        lines=lines,
        total=total,
        quarters=_quarters(total),
        scenarios=scenarios,
        computers_in_use=seats_now,
        by_class=by_class,
        bands=bands,
        spares=spares,
        not_seen=not_seen,
        unreviewed=unreviewed,
        must_replace=sorted(must, key=lambda c: (-(c.age or 0), c.name.lower())),
        growth=history,
        net_seats=nets,
        predicted_seats=predicted,
        steady=steady,
        actuals=actual_spend(purchases or []),
        replacements_last_year=bought,
        gaps=gaps,
        gap_links=links,
        missing_prices=missing,
        upgrades=sorted(upgrades, key=lambda c: (-(c.age or 0), c.name.lower())),
        left_out=sorted(left_out, key=lambda c: c.name.lower()),
        early=early,
        skipped=sorted(skipped, key=lambda c: c.name.lower()),
        plan_start=start,
        plan=chosen.key if chosen else "",
        plans=[p.as_dict() for p in found.values()],
    )
