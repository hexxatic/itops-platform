# Reports module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `reports` module.

## Purpose

Datasets and the dashboards built on them (ADR 0016). A dataset is a table of rows with
typed columns, written either by a connector (synced) or by people (typed in, or pasted from
a spreadsheet). A report is a dataset narrowed by fixed filters and shown as tiles, a trend
chart, breakdowns and a detail list people can drill into and download as Excel or CSV.
Not the place for forecasting logic (the `budget` module does that, reading datasets here),
and not a search index (rows aren't in search).

## Contract (keep in sync with `src/itops_reports/manifest.toml`)

- **Permissions:** `reports.read`; `reports.write` (add, change, delete rows of typed-in
  datasets, audited; AI may draft reports); `reports.publish` (**human-only**: create and
  change datasets and reports, publish AI drafts); `reports.costs` (`grant = "group"`: see
  money columns; never reaches AI); `reports.types.draft`, `reports.types.approve`
  (**human-only**): dataset column types
- **Areas (ADR 0012):** `dataset` and `report` (keys are their keys). A hidden dataset hides
  its reports.
- **Events published:** `reports.rows_changed` (people changed rows of a typed-in dataset)
- **Services provided:** `reports.ensure_dataset` (a connector creates its synced dataset if
  missing; leaves an existing one alone), `reports.upsert` (connectors only: one row by the
  source's key; needs `owns = { "reports.rows" = ["*"] }`; unchanged rows skipped; dry runs
  write nothing), `reports.rows` (a dataset's rows for another module, as the viewer sees
  them), `reports.describe_area`, `reports.area_entities`
- **Jobs:** `retention` (daily 03:30): deletes rows older than their dataset's retention
- **MCP tools:** `reports_list_reports`, `reports_list_datasets`, `reports_get_report`
  (read; money never included); `reports_draft_report` (`reports.write`; a draft a person
  publishes)
- **Depends on:** core only

## Module-specific notes

- **Columns are a record type** (`starter_types.json`: File change, Purchase, Employee (no
  names)). Admins add more at `/reports/types`. A dataset's type can't change once it has rows.
- **Rows** store values as JSON (`reports.rows.values`) plus `row_time` (from the dataset's
  time column, for windows, trends and retention). Reports read a dataset's rows once
  (capped at `engine.MAX_ROWS`) and filter, group and chart them in Python (`engine.py`, no
  database there). Keep datasets bounded with retention.
- **Report definitions** are JSON (`engine.ReportConfig`), validated against the dataset's
  columns on every save and view. People with `reports.publish` save straight to `config`;
  an AI connection saves `draft_config`, shown on the index and the report page until a
  person publishes or discards it.
- **Money columns** are stripped in `service.columns()` for anyone without `reports.costs`
  and for every AI connection: rows, measures (shown as "—"), exports, tools. Saving a row
  keeps money the person couldn't see.
- **Pasting** reads tab-separated (or CSV) text with a header row naming columns by label
  or key; every row is validated and shown as a preview before anything is saved.
- **Viewer filters** on a report: a time window, a text search, and drill filters (`w=
  field|op|value`, from clicking a breakdown entry; folder entries use `starts_with`).
- Adding rows through AI isn't built (ADR 0016 lists `reports_propose_rows` for later).
- Code layout: `app.py` (the Module object), `models.py`, `engine.py` (definitions and
  evaluation), `service.py` (rules, services, the job), `views.py`, `tools.py`.
