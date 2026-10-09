# Docs module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `docs` module.

## Purpose

IT documentation (the successor to Loretome, retired 2026-09-24; see ADR 0003).
Section-based documents (Heading / Text / Step / HTML blocks), drafts and publishing,
immutable version history, collections and tags. It is **not** a password store:
credentials stay in 1Password and are only referenced.

## Contract (keep in sync with `src/itops_docs/manifest.toml`)

- **Permissions:** `docs.read`, `docs.write`, `docs.publish` (**human-only**: publish,
  discard, archive, restore), `docs.types.draft` + `docs.types.approve` (**human-only**):
  document types (ADR 0008), admin-only by construction, `docs.collections.manage`
  (**human-only**, admin-only): create, rename, reorder, delete collections (deleting
  moves any documents to another collection first; nothing is deleted with it)
- **Events published:** `docs.document_published`, `docs.document_archived`
- **Services provided:** `docs.lookup` (documents for link pickers and labels),
  `docs.related` (published documents whose fields link to a record), `docs.link_targets`;
  for connectors (ADR 0010, `sync.py`): `docs.ensure_collections`, `docs.source_status`,
  `docs.save_file`, `docs.upsert`; for retiring a document type (`retire.py`):
  `docs.type_usage`, `docs.move_type`, `docs.archive_type`
- **Depends on:** core; `assets` optional (link fields to assets, the "Assets" card on a
  document)

## Module-specific notes

- **Documents hold identity, versions hold content.** Versions are immutable; every
  save is a new draft version; publishing stamps the newest version and repoints
  `documents.current_version_id`. Never update a version's content in place.
- **All changes go through `service.py`** (pages today, MCP tools next) so the draft
  rule lives in one place. `service` also refuses publish/discard/archive for AI
  principals as a second lock behind the `docs.publish` permission.
- Content is the core block format (`itops.sdk.content`), stored as JSON in
  `docs.versions.content` and normalized (sanitized) on every save.
- **Every write is audited** (`service._audit`, ADR 0006): created, edited,
  details_changed, published, draft_discarded, deleted, archived, restored. A new
  write path in `service.py` records its own entry.
- **Attachments** live on the version (`versions.attachments`, JSON [{id, filename,
  size, mime}]) like fields: adding/removing one makes a draft, readers see the published
  list, content-only saves keep it. Files go through `module.files.save(attachment=True)`
  (extension allowlist, 50 MB, always served as downloads) and are claimed by the document.
- **Document types** (ADR 0008): `documents.type_key` names a core record type;
  field values live in `versions.fields` (JSON), versioned with the content, so field
  edits are drafts. Drafts may leave required fields empty; `publish` refuses until
  they're filled. Published values are rebuilt into `docs.document_fields` (queryable)
  and a "Details" search passage (credential fields never go into search) in `_index`.
  A typed document may have no sections (its fields can be the whole record). Content
  saves without `fields` keep the previous values.
- Screenshots use the core file store; `service` claims the file ids a version uses.
- The editor is the core `ui.section_editor` island; the reader uses `ui.content` and
  `ui.toc`. Don't add module JS or CSS.
- **Synced documents (ADR 0010, `sync.py`):** connectors create and update documents only
  through these services, which refuse to run outside a connector run and write nothing in
  a dry run. A document whose newest version a person wrote is never overwritten by a
  later run ("kept"), so people can clean imported drafts (e.g. move a password to
  1Password) before publishing them. Collections are addressed by slug.
- Code layout: `app.py` (the Module object), `models.py`, `service.py`, `views.py`,
  `tools.py`, `sync.py`.
