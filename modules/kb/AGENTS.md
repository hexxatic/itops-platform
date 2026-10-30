# How-tos module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `kb` module.

## Purpose

Staff find, read and search IT's how-tos on the **help site** (ADR 0027), and say whether
each one helped; IT sees which ones need fixing. The how-tos are Docs' own: published
documents a publisher shows on the help site, each for everyone or an audience (ADR 0028). kb reads them live through Docs'
services and never copies or edits them. It does not author articles, has no drafts and
no write MCP tools.

## Contract (keep in sync with `src/itops_kb/manifest.toml`)

- **Site:** `help`
- **Permissions:** `kb.read` (`grant = "everyone"`: read, leave feedback),
  `kb.feedback.manage` (`grant = "group"`: what people said)
- **Events published:** `kb.feedback_left`
- **Events used:** `docs.document_published`, `docs.document_archived`,
  `docs.collection_changed`, `docs.help_site_changed` (search catches up at once)
- **Services provided:** none
- **Services used:** `docs.howto_collections`, `docs.howtos`, `docs.howto` (as the
  person looking: an audience they're not in hides its how-tos), `docs.howto_index` (every
  how-to, for search) (optional: without Docs there are no how-tos)
- **Jobs:** `index`, every 15 minutes (search; what a collection's restriction changes)
- **MCP tools:** `kb_search`, `kb_get_article` (read)

## Module-specific notes

- Images and attachments are Docs' files (`/files/<id>`); Docs opens them to everyone
  (`docs.howto`) while the document is a how-to (`shared_file_permission`), and checks
  its audience (`visible_entity`).
- Search entries are kb's (kind `article`, keyed by the Docs slug, `kb.read`), so staff
  search the help site and IT search Docs. Each carries its Docs audience's area
  (`area='docs:audience:<id>'`), so search hides it from people outside the audience; the
  sync reads `howto_index`, never the viewer-filtered services, so whoever triggers it sees
  every how-to. `kb.indexed` records the version and area each was built from;
  `service.sync` touches only what changed.
- Feedback is one answer per person per how-to (a new one replaces it), refused via AI,
  and kept when the how-to is withdrawn.
