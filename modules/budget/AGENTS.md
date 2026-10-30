# Budget module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `budget` module.

## Purpose

The device budget (ADR 0016): a rolling 12-month forecast of what computers and peripherals
will cost, for an organisation with no refresh policy that replaces devices as needed. It
reads the fleet from Assets and hiring and purchase history from Reports datasets, and
multiplies by a short price list. Every line says what it rests on (data, partly
estimated, estimate) and missing data never blocks it: the page lists what would firm it
up. Not purchasing, invoicing or approvals of individual purchases.

## Contract (keep in sync with `src/itops_budget/manifest.toml`)

- **Permissions** (all `grant = "group"`: every figure is money; admins hold them all):
  `budget.read`, `budget.write` (assumptions, price list, saving versions),
  `budget.approve` (**human-only**: mark a saved version adopted; approve or reject
  proposed changes)
- **Events published:** `budget.version_adopted`
- **Services provided:** `budget.dashboards` (listed on the Reports page for people with
  `budget.read`)
- **MCP tools:** `budget_forecast_counts` (counts, rates and gaps; the forecast's amounts
  stay on the page), `budget_list_prices`, `budget_get_assumptions`, `budget_list_proposals`
  (`budget.read`); `budget_propose_price`, `budget_propose_price_removal`,
  `budget_propose_assumptions` (`budget.write`, proposals only)
- **Depends on:** core; `assets` optional (`assets.fleet`), `reports` optional
  (`reports.rows` for the HR and purchase-history datasets). Without them the forecast
  says what's missing.

## Module-specific notes

- **`engine.py` is pure arithmetic, never AI**: `forecast(fleet, people, purchases, prices,
  assumptions, today)` → `Forecast`. Test it with plain values; it's what a saved version
  freezes (`Forecast.as_dict()`).
- **Lines:** must replace (in-use computers past `must_replace_age`, or on an OS in
  `unsupported_os` whose hardware can't take a supported one), upgrade in place (on an
  unsupported OS, hardware ready for a supported one: a count, no money; ADR 0018). Hardware
  is judged by Assets' OS readiness (`os_ready` in `assets.fleet`, ADR 0019); only when it's
  unknown does `upgradeable_models` decide (`Computer.judged` = hardware | model),
  replacements as needed (a rate per age band over the rest), new seats (average net seats
  a year from HR history through the seat rule, less spares, each a blended computer plus
  a kit), peripherals wearing out (a % of a kit per computer in use), contingency (% on
  top). Scenarios scale failures and use the slowest/fastest year.
- **Five-year plan** (ADR 0020, `plan.py`, pure): `plans(...)` runs minimum, even, cycle,
  amount and lease over five calendar years from each computer's age. Every plan pays the
  floor (must go + failures); they differ in what they replace early, oldest first. Even is the
  lowest steady amount no year goes over, buying early only what comes due within the plan.
  Reuse (`reuse_pct` of net leavers) cuts purchases; prices rise `price_rise_pct` a year after
  year 1. **Year 1 of the chosen plan (`plan`) is the budget**: lines *Replaced early*,
  *Reused* (negative), *Lease payments* (purchase lines then show `Line.shown` = "Leased").
  `Forecast.plan_start/plan/plans` are frozen in versions; older snapshots show the
  steady-policy card. The plan form posts to `/api/budget/plan` (`budget.write`).
- **Left out:** in-use computers whose model is in `equipment_models` (their own
  equipment: machine controllers, gauges, room systems) or that a person excluded with a
  reason (`budget.exclusions`, by asset id; `budget.write`, audited, never AI) aren't in the
  fleet being budgeted. Every one is listed with why under *Not in this budget*, on the page,
  each version and the summary; nothing is dropped silently.
- **Replacement list** (ADR 0020 amendment; its own page `/budget/replacements`, a summary card
  on the Budget page, in full on each saved version): the budget year's replacements by name, must go
  (`Forecast.must_replace`) then replaced early by the plan (`Forecast.early`, the oldest of
  each class per `PlanYear.early_by_class`); failures stay a count. A person skips one to next
  year with a reason (`budget.skips`, per budget year, `budget.write`, audited, never AI):
  `Computer.skipped` holds it out of year 1 (no cost, no failure, no early pick) and the plan
  makes it must go in year 2; the next year it's listed again with `skipped_before`. Forms on
  `/budget/` that fail come back through `_form_back`, which keeps the `#part` after the error.
- **Summary** (`/budget/v/{id}/summary`, ADR 0018): a saved version's one page for whoever
  agrees the budget, generated from its snapshot (never typed): total, a sentence per line,
  what it buys and defers, quarters (must-replace first), the scenario range, Excel. Older
  snapshots without `upgrades`/`left_out` still render.
- **Classes (ADR 0025):** each computer is `Computer.cls`, one of `CLASSES` (standard_laptop,
  standard_desktop, engineering_laptop, engineering_desktop) from Assets' `computer_class`;
  without one, the standard class of its kind (`classified=False`, counted in the gaps). Prices
  are per class (`PRICE_CATEGORIES`); old laptop/desktop entries and inputs are aliases of the
  standard ones (`PRICE_ALIASES`). No engineering price: priced as the standard one, said in
  the gaps. Old versions' laptop/desktop keys still render (`CLASS_LABELS`).
- **Computers in use:** a Device whose kind is in `laptop_kinds`/`desktop_kinds`, not
  Retired/Inactive/Ignored/Spare, and either Active or seen within `in_use_days`. **Who saw
  it is Assets' call** (`assets.fleet(in_use_days=...)` gives `seen_by`: a scan, else AD;
  ADR 0017): Budget doesn't judge sources. `Computer.seen_by` keeps it, and one gap counts the
  computers Assets lists under *Sources disagree*, linking there. Age is `assets.fleet`'s `acquired` (purchase date, else warranty end minus
  3 years, else *In service since*, a person's estimate, else an HP serial's build week,
  else when it joined the domain);
  `Computer.age_basis` says which, and estimated ages are listed under what would firm it up.
- **Inputs by column key:** the HR dataset uses Reports' starter type *employee*
  (`pay_type`, `dept_group`, `hire_date`, `rehire_date`, `termination_date`, ...), the
  purchase-history dataset the starter type *purchase* (`date`, `category`, `quantity`,
  `unit_cost`, `total`, `reason`). Dataset keys are assumptions (`hr-employees`,
  `device-purchases`).
- **Budget's own tables:** `budget.settings` (the assumptions as one JSON document),
  `budget.prices`, `budget.versions` (title, status saved/adopted, total, snapshot JSON),
  `budget.proposals` (kind, the proposed values as JSON, note, pending/approved/rejected),
  `budget.exclusions` (asset id, name, reason, who and when), `budget.skips` (asset id, name,
  budget year, reason, why it was due, who and when).
  One version is adopted at a time; actual spend since it was saved comes from purchase
  history.
- **AI never changes figures directly**: the service refuses via-AI principals on every
  write. AI proposes prices and assumption changes instead; each proposal is checked like a
  real save, waits on the Assumptions and prices page, and is applied as the person who
  approves it (`budget.approve`).
