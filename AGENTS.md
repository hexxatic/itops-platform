# IT-Ops platform: agent instructions

These are the repository rules every module follows. Each module's own `AGENTS.md` adds
what is specific to that module. `itops lint` enforces the rules below in pre-commit and
CI; a violation says how to fix it. A rule can be bypassed on purpose with a visible
exemption on the same line that says why:

    import itops.core.db  # itops: allow import-core -- <reason>

## Rules

1. Modules import only `itops.sdk`. Core internals are off limits. If the SDK is
   missing something, add it to the SDK.
2. Modules never import each other. They talk through events and
   `module.services.get()`, and declare what they provide and require in
   `manifest.toml`.
3. Modules ship no CSS or JS. Pages use core components and design tokens (`/_design`
   lists them). No `<style>` blocks, no `style=` attributes, no literal colors, no
   inline `<script>`. Behavior comes from htmx and Alpine attributes or an approved
   core island.
4. A theme's `theme.css` overrides design tokens and nothing else.
5. Every write is audited and nothing is deleted in place. Publish, retire and archive
   are the verbs. Permissions marked `human_only` are never held by an AI principal.
6. `core/` and `modules/` are exported publicly. Company specifics (names, hosts,
   domains, addresses) live under `private/`, which is not exported.
7. All changes to a module's data go through its `service.py` so the draft and audit
   rules live in one place. Pages and MCP tools both call the service.
8. Keep each module's `AGENTS.md` contract section in sync with its `manifest.toml`.

## Working here

- `uv sync --all-packages` installs everything. `uv run itops serve --reload` for dev.
- `uv run itops new module <name>` scaffolds a module that already follows the rules.
- `uv run pytest -q` runs core, module and integration tests. Database tests need a
  local SQL Server.
- `uv run itops lint` runs the rules above. ruff and pyright (strict) run in pre-commit.
- Docstrings reference architecture decision records by number (ADR 0003, 0010, and so
  on). The ADRs live in the internal repository; the docstrings carry the reasoning.
