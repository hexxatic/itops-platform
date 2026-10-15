# Contracts module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `contracts` module.

## Purpose

IT contracts (ADR 0014): software, support, cloud, telecom and hardware leases. Each
contract's terms (dates, cost, PO number, seats, the signed PDF), renewals that come due
on time, and a person's decision to renew or cancel. Not company-wide contracts (legal,
customers, leases), not purchasing or invoicing.

## Contract (keep in sync with `src/itops_contracts/manifest.toml`)

- **Permissions** (all `grant = "group"`: roles don't grant them, `ITOPS_PERMISSION_GROUPS`
  does; admins hold them all): `contracts.read`, `contracts.write`, `contracts.approve`
  (**human-only**: renew, cancel, accept AI drafts, archive), `contracts.costs` (see and
  change costs)
- **Events published:** `contracts.renewal_due` (120/90/60/30 days before cancel-by),
  `contracts.renewal_overdue` (weekly past cancel-by), `contracts.renewed`,
  `contracts.cancelled`. Meant for `notify` (email reminders) once it exists.
- **Services provided:** `contracts.lookup`, `contracts.related`, `contracts.link_targets`,
  `contracts.upsert` (connector runs: creates a contract once, copies its files; never
  overwrites later edits)
- **Jobs:** `renewals` (daily 06:00): marks terms due, publishes the reminders, marks ended
  undecided terms lapsed (unless the contract auto-renews)
- **MCP tools:** `contracts_list_contracts`, `contracts_get_contract`,
  `contracts_renewals_due` (read); `contracts_draft_contract`, `contracts_update_draft`
  (`contracts.write`; drafts only)
- **Depends on:** core; `docs` and `assets` optional (the vendor document and covered assets
  as links)

## Module-specific notes

- **A contract and its terms.** `contracts.contracts` holds what stays the same;
  `contracts.terms` one row per period. The newest term is the current one; renewing marks
  it `renewed` and adds the next. Terms are changed in place (people edit directly, every
  change audited), not versioned.
- **Cancel-by date = current term's end minus `notice_days`**, computed, never stored.
  Reminders, lists and the "days left" badges all use it.
- **States:** `active` -> `due` (job: within 120 days of cancel-by) -> `negotiating`
  (a writer) -> `renewed` / `cancelled` (an approver) or `lapsed` (job: ended, undecided,
  not auto-renewing). `terms.notified` remembers which reminders went out; changing a
  term's dates resets it.
- **Costs never reach AI**: tools don't return or take them; pages show them only with
  `contracts.costs` (`service.can_see_costs`), and saving a term without that permission
  keeps the cost as it was.
- **AI drafts:** a via-AI create makes a `draft` contract (not in lists, search or
  reminders); `service` refuses AI on everything but drafts; a person accepts or discards.
- **Term files are not in search** on purpose: invoices and quotes carry prices, and search
  results reach AI connections. The local model reads them for suggestions (ADR 0015).
- **Suggestions from a term's files** (ADR 0015, feature `contracts.suggest`): "Suggest
  values from <file>" on the contract page starts a run; `/contracts/t/<term>/suggestions/<run>`
  reviews it next to the document. Fields: dates, cost/billing (only with
  `contracts.costs`), seats, PO, auto-renew, and the notice period, which is never
  pre-filled. `service.apply_suggestions` saves what's ticked and audits it.
- **Start from a file** (New contract, when `contracts.suggest` is on): the upload is saved
  unclaimed and read with `service.new_contract_fields` (name, vendor, category, then the
  term fields) as a run on entity `("new_contract", <file id>)`. `/contracts/from-file/<run>`
  shows the whole new-contract form filled from the suggestions, each with its quote, next to
  the document; creating it claims the file onto the first term and marks the run reviewed
  (a file makes one contract). By hand, the New contract form takes files too, and a
  contract page always shows a Files card with "Add files".
- **Owner** is a UPN from `module.people.options(holding="contracts.read")`.
- **Seats in use** are typed in for now; ADR 0014 wants them from a shared source (e.g. a
  license record in Assets fed by connectors) later, with typing as the fallback.
- Renewing also works from `lapsed` (it ended undecided, then was renewed after all).
- Code layout: `app.py` (the Module object), `models.py`, `service.py` (rules, the job,
  services), `views.py`, `tools.py`.
