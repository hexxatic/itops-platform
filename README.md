# IT-Ops platform

A small, modular platform for internal IT applications: a core (sign-in, permissions, audit
trail, search with local embeddings, files, record types, connectors, tool actions, MCP for
AI clients with OAuth) plus drop-in modules (`modules/docs`, `modules/assets`) that share the
plumbing and look identical.

This is an export of the generic part of a larger internal repository: company-specific
modules, configuration and deployment settings are not included.

```
uv sync --all-packages      # install core + modules
uv run itops serve          # http://localhost:8800  (/_design = components)
uv run itops new module x   # scaffold a module that follows the rules
uv run pytest -q            # tests (SQL Server needed for the database tests)
```

Exported 2026-10-09 from 346fa68.
