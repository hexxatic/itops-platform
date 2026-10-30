"""Five-year plans (ADR 0020): how the fleet is replaced, year by year, for each way of
paying for it. Pure arithmetic like the rest of the engine, never AI.

Every plan pays the same floor each year: computers that must go (too old, or on a system
their hardware can't leave) and the expected failures, a rate per age band. Plans differ in
what they replace on top, oldest first:

- **minimum**: nothing more;
- **even**: the lowest steady yearly amount (in today's money) that leaves no spike: computers
  that would come due within the five years anyway are replaced a year or two early, so
  about the same money as Minimum is spread evenly;
- **cycle**: about one in ``cycle_years`` computers a year;
- **amount**: what a typed yearly amount buys (the floor is still paid when it's more);
- **lease**: the cycle's replacements, leased: monthly payments instead of purchases.

Computers left by people who leave are reused where they can be (``reuse_pct`` of the net
leavers a year), so fewer are bought. Prices rise ``price_rise_pct`` a year after year 1.
A computer of unknown age keeps its band's rate, is never too old and is replaced early last.
A computer a person skipped to next year sits out year 1 (it neither fails nor is replaced
early) and, if it was due, must go in year 2.
Ages are as of each plan year's 1 January (a computer's ``age_then`` in year 1, a year more
each year after), as the budget counts them, so year 1 is the budget.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from itops_budget.engine import Assumptions, Computer, band_of

YEARS = 5
OLD = 7  # "7 or more years old": how the plans compare what they leave behind
REUSED_AGE = 3  # a leaver's computer, given to someone whose computer is due
PLANS: dict[str, tuple[str, str]] = {
    "minimum": (
        "Minimum",
        "Replace only what must go (too old, or can't run a supported system) and what fails.",
    ),
    "even": (
        "Even spend",
        "About the same money as Minimum, spread evenly: computers that come due within the "
        "five years are replaced a year or two early, so no year carries a spike.",
    ),
    "cycle": ("Replacement cycle", "Replace about one computer in N every year, oldest first."),
    "amount": (
        "Yearly amount",
        "What a set amount a year buys, oldest first. Failures and computers that must go are "
        "always replaced, even past the amount.",
    ),
    "lease": (
        "Lease",
        "The replacement cycle's computers, leased: paid monthly, returned at the end of the "
        "term, never owned.",
    ),
}

Group = tuple[str, int | None]  # (laptop | desktop, whole years old or None)


@dataclass
class PlanYear:
    year: int
    must: float  # computers that must go
    failures: float  # expected to fail or not keep up
    early: float  # replaced early by the plan
    reused: float  # leavers' computers given out instead of buying
    bought: float  # computers bought (or newly leased)
    leased: float  # computers on lease at the end of the year
    must_cost: float
    failure_cost: float
    early_cost: float
    reuse_saving: float
    lease: float  # lease payments
    seats: float
    peripherals: float
    contingency: float
    total: float
    over: float  # spent beyond the yearly amount (amount plan)
    old: float  # computers 7 or more years old at the end of the year
    # Replaced early, by class: the budget names that many of each class's oldest.
    early_by_class: dict[str, float] = field(default_factory=dict[str, float])


@dataclass
class Plan:
    key: str
    name: str
    about: str
    years: list[PlanYear] = field(default_factory=list[PlanYear])

    @property
    def total(self) -> float:
        return round(sum(y.total for y in self.years), 2)

    @property
    def peak(self) -> PlanYear:
        return max(self.years, key=lambda y: y.total)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "about": self.about,
            "total": self.total,
            "years": [asdict(y) for y in self.years],
        }


def start_year(today_year: int, today_month: int) -> int:
    """The first year a plan budgets: next year from July (budgets are set ahead), else this
    year."""
    return today_year + 1 if today_month >= 7 else today_year


def _rate(age: int | None, rates: dict[str, float]) -> float:
    return max(0.0, rates.get(band_of(age), 0.0)) / 100


def _simulate(
    key: str,
    in_use: list[Computer],
    price: dict[str, float],
    a: Assumptions,
    start: int,
    *,
    seats: float,
    peripherals: float,
    net_leavers: float,
    level: float = 0.0,
) -> Plan:
    name, about = PLANS[key]
    plan = Plan(key, name, about)
    fleet: dict[Group, float] = {}
    must0: dict[Group, float] = {}
    held: dict[Group, float] = {}  # skipped this year, not due
    must_next: dict[str, float] = {}  # skipped this year, due: must go next year
    for c in in_use:
        g: Group = (c.cls, math.floor(c.age_then) if c.age_then is not None else None)
        if c.skipped and c.reasons:
            must_next[c.cls] = must_next.get(c.cls, 0.0) + 1
            continue
        target = held if c.skipped else must0 if c.reasons else fleet
        target[g] = target.get(g, 0.0) + 1
    size = len(in_use)
    class_size = {cls: float(sum(1 for c in in_use if c.cls == cls)) for cls in price}
    total_n = sum(fleet.values()) + sum(must0.values())
    blended = (
        sum(price[cls] * n for (cls, _), n in [*fleet.items(), *must0.items()]) / total_n
        if total_n
        else sum(price.values()) / len(price)
    )
    rise = max(0.0, a.price_rise_pct) / 100
    pct = max(0.0, a.contingency_pct) / 100
    cycle = max(1, a.steady_years)
    reusable = max(0.0, net_leavers) * max(0.0, min(100.0, a.reuse_pct)) / 100
    leased: dict[str, float] = {}
    for y in range(YEARS):
        infl = (1 + rise) ** y

        def remove(
            g: Group, n: float, into: dict[str, float], fleet: dict[Group, float] = fleet
        ) -> float:
            fleet[g] -= n
            into[g[0]] = into.get(g[0], 0.0) + n
            return n

        must: dict[str, float] = {}
        if y == 0:
            for g, n in must0.items():
                must[g[0]] = must.get(g[0], 0.0) + n
        else:
            for g in list(fleet):
                if g[1] is not None and g[1] >= a.must_replace_age:
                    remove(g, fleet[g], must)
            if y == 1:
                for cls, n in must_next.items():
                    must[cls] = must.get(cls, 0.0) + n
        fails: dict[str, float] = {}
        for g in list(fleet):
            remove(g, fleet[g] * _rate(g[1], a.replace_rates), fails)
        floor = sum(must.values()) + sum(fails.values())

        def cost(counts: dict[str, float], infl: float = infl) -> float:
            return sum(n * price[cls] for cls, n in counts.items()) * infl

        # What the plan replaces early, oldest first: a number of computers (cycle, lease),
        # or what's left of the year's money after the floor (even, amount).
        unit = blended * infl
        early: dict[str, float] = {}
        order = sorted(fleet, key=lambda g: (g[1] is None, -(g[1] or 0)))
        if key in ("cycle", "lease"):
            extra = max(0.0, size / cycle - floor)
            for g in order:
                if extra <= 1e-9:
                    break
                extra -= remove(g, min(fleet[g], extra), early)
        elif key == "even" or (key == "amount" and a.plan_amount > 0):
            spend = level * infl if key == "even" else a.plan_amount
            if key == "even":  # only computers that come due within the plan anyway
                left = YEARS - 1 - y
                order = [g for g in order if g[1] is not None and g[1] + left >= a.must_replace_age]
            money = (
                spend / (1 + pct)
                - (seats + peripherals) * infl
                - cost(must)
                - cost(fails)
                + min(reusable, floor) * unit
            )
            for g in order:
                each = price[g[0]] * infl
                if money < each * 1e-6:
                    break
                money -= remove(g, min(fleet[g], money / each), early) * each

        replaced = {
            cls: must.get(cls, 0.0) + fails.get(cls, 0.0) + early.get(cls, 0.0) for cls in price
        }
        n_replaced = sum(replaced.values())
        reused = min(reusable, n_replaced)
        share = reused / n_replaced if n_replaced else 0.0
        bought = {cls: n * (1 - share) for cls, n in replaced.items()}

        lease = 0.0
        if key == "lease":
            for cls, n in bought.items():  # never more on lease than there are computers
                leased[cls] = min(leased.get(cls, 0.0) + n, class_size[cls])
            lease = sum(
                n * price[cls] / cycle * (1 + max(0.0, a.lease_markup_pct) / 100)
                for cls, n in leased.items()
            )
            lease *= infl
            computers = 0.0
        else:
            computers = cost(replaced) - reused * unit
        sub = computers + lease + (seats + peripherals) * infl
        contingency = sub * pct
        total = sub + contingency
        over = 0.0
        if key == "amount" and a.plan_amount > 0:
            over = max(0.0, total - a.plan_amount)

        # A year older; new and reused computers join, and after year 1 the skipped ones.
        aged: dict[Group, float] = {}
        joining: list[tuple[Group, float]] = [*fleet.items(), *(held.items() if y == 0 else [])]
        for (cls, age), n in joining:
            if n > 1e-9:
                g: Group = (cls, age + 1 if age is not None else None)
                aged[g] = aged.get(g, 0.0) + n
        for cls in price:
            if bought[cls]:
                aged[(cls, 1)] = aged.get((cls, 1), 0.0) + bought[cls]
            if replaced[cls] * share:
                g = (cls, REUSED_AGE + 1)
                aged[g] = aged.get(g, 0.0) + replaced[cls] * share
        fleet = aged
        old = sum(n for (_, age), n in fleet.items() if age is not None and age >= OLD)
        plan.years.append(
            PlanYear(
                year=start + y,
                must=round(sum(must.values()), 2),
                failures=round(sum(fails.values()), 2),
                early=round(sum(early.values()), 2),
                reused=round(reused, 2),
                bought=round(sum(bought.values()), 2),
                leased=round(sum(leased.values()), 2),
                must_cost=round(cost(must), 2),
                failure_cost=round(cost(fails), 2),
                early_cost=round(cost(early), 2),
                reuse_saving=round(reused * unit, 2),
                lease=round(lease, 2),
                seats=round(seats * infl, 2),
                peripherals=round(peripherals * infl, 2),
                contingency=round(contingency, 2),
                total=round(total, 2),
                over=round(over, 2),
                old=round(old, 2),
                early_by_class={cls: round(n, 2) for cls, n in early.items() if n > 1e-9},
            )
        )
    return plan


def plans(
    in_use: list[Computer],
    price: dict[str, float],
    a: Assumptions,
    start: int,
    *,
    seats: float,
    peripherals: float,
    net_leavers: float,
) -> dict[str, Plan]:
    """Every plan over five years, from the computers in use (``reasons`` set on the ones
    that must go now), prices by class, the yearly cost of new seats and peripherals in
    today's money, and the net leavers a year (whose computers can be reused)."""
    kw = {"seats": seats, "peripherals": peripherals, "net_leavers": net_leavers}
    out = {k: _simulate(k, in_use, price, a, start, **kw) for k in PLANS if k != "even"}
    rise = max(0.0, a.price_rise_pct) / 100

    def peak(p: Plan) -> float:  # the highest year, in today's money
        return max(y.total / (1 + rise) ** i for i, y in enumerate(p.years))

    # Even: the lowest yearly amount (today's money) that no year goes over. Minimum's highest
    # year always works (buying early only lowers later years), so search below it.
    low, high = 0.0, peak(out["minimum"])
    for _ in range(40):
        level = (low + high) / 2
        fits = peak(_simulate("even", in_use, price, a, start, level=level, **kw)) <= level + 0.5
        low, high = (low, level) if fits else (level, high)
    out["even"] = _simulate("even", in_use, price, a, start, level=high, **kw)
    return {k: out[k] for k in PLANS}
