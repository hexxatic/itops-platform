# Assets module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `assets` module.

## Purpose

IT inventory (ADR 0008, ADR 0009): devices, SSL certificates, contacts, locations, domains
and IP addresses, each an admin-defined record type on the core type builder. It holds
facts about things; written procedures and runbooks belong in Docs (which links here).
It is **not** a password store: secrets stay in 1Password (credential fields hold
references only).

## Contract (keep in sync with `src/itops_assets/manifest.toml`)

- **Permissions:** `assets.read`, `assets.write` (people edit directly), `assets.publish`
  (**human-only**: approve/reject AI proposals, delete records), `assets.types.draft` +
  `assets.types.approve` (**human-only**): asset types, admin-only by construction
- **Events published:** `assets.record_created`, `assets.record_updated`,
  `assets.record_deleted`
- **Services provided:** `assets.lookup` (records for link pickers and labels:
  `lookup(ids=...)` or `lookup(kind=..., query=...)` → [{id, kind, name, url}]),
  `assets.related` (assets whose fields link to a record), `assets.link_targets`,
  `assets.upsert` (connectors only, ADR 0010: create or update from a source record),
  `assets.type_usage` and `assets.move_type` (retiring a type; synced records stay)
- **MCP tools:** `assets_search`, `assets_list_records`, `assets_get_record`,
  `assets_list_proposals` (read); `assets_propose_record`, `assets_propose_change`
  (`assets.write`; they store proposals, never change an asset)
- **Depends on:** core; `docs` optional (link fields to documents, the "Documents" card on
  a record)

## Module-specific notes

- **Every record = a type + a name + field values** (`records.fields`, JSON). Values are
  validated against the type's *published* definition on every save; required fields are
  enforced (no drafts for people).
- **All changes go through `service.py`** (pages, MCP tools, the IT Glue import). People
  edit directly and every change is audited with before/after values. **AI changes are
  `propose(...)`**: a person approves (applies) or rejects them; the service refuses AI
  principals for `decide` and `delete_record` as a second lock.
- **Synced records (ADR 0010):** `upsert` creates a record with every source value, then
  updates only the fields the connector owns (people own the rest), skips unchanged
  source records (fingerprint) and writes nothing in a dry run. Outside a connector run,
  `update` puts owned fields back to their current values and `propose` refuses changes
  to them. The record page shows "Synced from ..." and flags records gone from the source.
- `_index` rebuilds `assets.record_fields` (queryable) and the core search entry after
  every change; link and credential fields never go into search text.
- **Starter types** ship in `starter_types.json`; the core seeds missing ones at startup
  and never overwrites existing ones (admins' changes stay).
- Code layout: `app.py` (the Module object), `models.py`, `service.py`, `views.py`,
  `tools.py`.
- **Pages** (`views.py`): `/assets/` (types + counts), `/assets/t/<type>` (list + search),
  `/assets/r/<id>` (record), `/assets/t/<type>/new`, `/assets/r/<id>/edit`,
  `/assets/proposals` (approval queue). Form actions are POSTs under `/api/assets/...`
  that redirect back, with problems as `?error=`. Link pickers for `assets.*` targets come
  from `service.lookup`; link values are `assets:<kind>:<id>`.
