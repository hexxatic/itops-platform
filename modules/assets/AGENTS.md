# Assets module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `assets` module.

## Purpose

IT inventory (ADR 0008, ADR 0009, ADR 0017): devices, monitors, SSL certificates, contacts,
locations, domains, IP addresses, software, service accounts and app credentials, each an
admin-defined record type on the core type builder, with one record per machine across
sources. It holds
facts about things; written procedures and runbooks belong in Docs (which links here).
It is **not** a password store: secrets stay in 1Password (credential fields hold
references only).

## Contract (keep in sync with `src/itops_assets/manifest.toml`)

- **Permissions:** `assets.read`, `assets.write` (people edit directly), `assets.publish`
  (**human-only**: approve/reject AI proposals, delete records), `assets.types.draft` +
  `assets.types.approve` (**human-only**): asset types, admin-only by construction;
  `assets.targets` (**human-only**, group-granted: the OS targets readiness checks against)
- **Areas (ADR 0012):** `type` (keys are type keys): admins restrict an asset type to AD
  groups or keep it out of AI on core's access page (the **Who sees these records** card on
  the type's page); core's type tools filter restricted types too
- **Events published:** `assets.record_created`, `assets.record_updated`,
  `assets.record_deleted`
- **Services provided:** `assets.waiting` (core's *Waiting for you*), `assets.lookup` (records for link pickers and labels:
  `lookup(ids=...)` or `lookup(kind=..., query=...)` → [{id, kind, name, url}]),
  `assets.related` (assets whose fields link to a record), `assets.link_targets`,
  `assets.upsert` (connectors only, ADR 0010: create or update from a source record;
  `duplicates=` other source ids for the same thing, whose records are merged into it;
  `identity=` {serial, macs, hostname} to match a machine another source already has),
  `assets.type_usage` and `assets.move_type` (retiring a type; synced records stay),
  `assets.describe_area` (names a type for the access page), `assets.area_entities`
  `assets.fleet` (ADR 0016: every device with kind, status, model, OS, dates, hardware and an
  `acquired` date from *Purchased* or *Warranty expires*, else a person's estimate in *In
  service since*, else the build week in an HP serial number, else *Joined the domain*
  (AD), with `age_basis` purchased | warranty | in_service | serial | joined, for Budget to
  count; plus AD's last sign-in and account state; *Ignored* left
  out; `os_ready` per device, ADR 0019), `assets.dashboards` (OS readiness, for the Reports page)
- **MCP tools:** `assets_search`, `assets_list_records`, `assets_get_record`,
  `assets_list_proposals`, `assets_os_readiness` (read); `assets_propose_record`, `assets_propose_change`
  (`assets.write`; they store proposals, never change an asset)
- **Depends on:** core; `docs` optional (link fields to documents, the "Documents" card on
  a record); `contracts` optional (the "Contracts" card on a covered record)

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
  to them, except an owned field the source left empty: people may fill it (a purchase date
  Lansweeper doesn't know), and the source's value replaces it once it sends one. A `sparse`
  value the source used to send and now leaves blank (someone cleared a wrong date there) is
  kept but becomes people's the same way (`_synced_filled`), so it can be corrected or cleared
  here; a merge carries that over. The edit
  form locks only owned fields that have a value. The record page shows "Synced from ..." and flags records gone from the source.
  Merging duplicates (`_merge_duplicates`) deletes only records no person ever changed and
  nothing outside Assets links to; assets linking to them are repointed, other systems'
  references to them move to the kept record (`module.refs.move`), and the deletion is
  audited with the last values.
- **One record per machine (ADR 0017):** `assets.identities` holds every record's
  normalised serial, MACs and host name (`identity.py`; placeholders like "Default string"
  left out), rebuilt by `_index`. The first time a source record is seen, `upsert(...,
  identity=)` links it to the record with the same serial, else a shared MAC, else the same
  host name (not when both have serials that differ: a replacement, reported). Several
  candidates: nothing is created (failed, "merge them first"). A record the same connector
  already syncs from another item is never a candidate.
- `_index` rebuilds `assets.record_fields` (queryable) and the core search entry after
  every change; link and credential fields never go into search text. The entry's area is
  the record's type (ADR 0012).
- **Restricted types (ADR 0012):** `service.hidden_types()` is applied in every read
  (`available_types`, `all_types`, `get`, `records`, `lookup`, `related`,
  `pending_proposals`) and `decide` refuses proposals of hidden types; a new read path must
  apply it too. `_get` is the unfiltered read (lookup's *Restricted* stubs). Connector runs
  see every type.
- **Starter types** ship in `starter_types.json`; the core seeds missing ones at startup
  and never overwrites existing ones (admins' changes stay).
- **Build dates (`dating.py`, pure):** an HP serial's 4th character is the year's last digit,
  the 5th and 6th the week. The decade is the latest that isn't in the future, ten years
  earlier when the processor (`processor_year`: Intel Core, Core Ultra, Ryzen) came out more
  than 5 years before. Used only when a device has no purchase or warranty date; worked out
  on read, never stored. On test (2026-10-20) it dated 92 of 92 undated HP computers, within
  three months of the known purchase date on 5 of 6 (the sixth's date was wrong).
- **In use and sources disagreeing (`evidence.py`, pure; ADR 0017 amended 2026-10-20):**
  `judge(values, today, days)` gives `seen_by` ("network": the device itself seen within the
  window, the later of an inventory scan's *Last seen* and the Sophos agent's *Last seen
  (Sophos)*, ADR 0017 amended 2026-10-23; else "ad": an enabled AD sign-in within it; else
  ""), `seen_on`/`seen_via` (that date and "a scan" | "Sophos"), and sentences where
  the sources or the status don't line up (seen but AD stale or disabled, AD only, Retired but
  still seen or its AD account still enabled, Active but seen by neither). A scan is trusted over AD (Justin: "I trust talking
  to a PC more than AD"). `same_serial` finds one machine held twice (placeholder serials
  ignored). `assets.fleet` carries both; `/assets/disagreements` lists them (linked from the
  home page when there are any) and each record page shows its own. Budget and anything else
  that counts what's in use reads the verdict instead of judging sources itself.
- **Computer classes (ADR 0025, `classes.py`, pure):** class = tier (the first of two
  admin-kept model-pattern lists to match, Engineering before Standard; `assets.computer_classes`,
  a missing row is the preset) x form factor (the kind, unless the model's name says laptop).
  A person's *Class* field overrides it; a model on neither list has no class. `/assets/classes`
  lists models in use and edits the lists (`assets.classes`, human-only, group-granted,
  audited); `assets.fleet` gives `computer_class` and `class_basis`.
- **Devices get their own views (`devices.py`, pure; the starter Device type only).** The
  list (`/assets/t/device`, `device_list.html` inside `type_list.html`) shows what's worked
  out per device (class, in use and who saw it, age and its basis, model, which sources know
  it: inferred from the record, since the SDK has no bulk source lookup), with filters, ready-made
  review views (`VIEWS`), sorting, paging (`PAGE_SIZE`) and a download of exactly what's
  filtered. A bulk change sets status and/or class on everything the filters match
  (`service.set_devices`: ordinary edits, audited, never AI; refused when the count changed
  since the page was shown). The record page puts the fields in `SECTIONS` (the rest under
  Other), each saying which source fills it, under an "At a glance" summary. Other types keep
  the generic pages. `assets.fleet(include_ignored=True)` feeds the list.
- **OS readiness (ADR 0019):** `readiness.py` (pure, never AI) checks a device's
  `processor`, `memory_gb`, `storage_gb` and `tpm` against each OS target
  (`assets.os_targets`) that replaces the system it runs: ready / not ready / unknown, with
  why. Targets are data an admin keeps (`PRESETS` holds Windows 11 from Microsoft's lists);
  the next Windows is a new target, not code. Worked out on read, never stored. Retired and
  Ignored devices aren't in reports.
- **`shared` fields (ADR 0024):** `upsert(shared={...})` writes fields the connector keeps in
  step both ways without owning them (a computer's status with AD): written in the run,
  audited as the connector, and still editable by people.
- **Upsert skips unknown fields:** values for fields the type doesn't have are left out and
  listed in the result's `ignored` (an install whose Device type predates a new field keeps
  syncing the rest).
- Code layout: `app.py` (the Module object), `models.py`, `service.py`, `views.py`,
  `tools.py`, `identity.py`, `readiness.py`, `dating.py`, `evidence.py`, `classes.py`, `devices.py`.
- **Pages** (`views.py`): `/assets/` (types + counts), `/assets/t/<type>` (list + search),
  `/assets/r/<id>` (record), `/assets/t/<type>/new`, `/assets/r/<id>/edit`,
  `/assets/proposals` (approval queue), `/assets/readiness` (OS readiness; Excel/CSV at
  `/api/assets/readiness/<id>`), `/assets/readiness/targets/<id|new>` (admins). Form actions are POSTs under `/api/assets/...`
  that redirect back, with problems as `?error=`. Link pickers for `assets.*` targets come
  from `service.lookup`; link values are `assets:<kind>:<id>`.
