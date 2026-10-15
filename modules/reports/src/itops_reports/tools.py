"""MCP tools for Reports (exposed as ``reports_<name>``).

Tools run *via AI* as the connected user: they see the reports and datasets that person may
see, **never money columns** (``reports.costs`` doesn't reach AI connections). An AI
connection can draft a report; a person with ``reports.publish`` publishes it. Adding rows
to typed-in datasets is for people (not yet a tool).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from itops_reports import service
from itops_reports.app import module
from itops_reports.engine import INTERVALS, MEASURES, OPS, evaluate, measure_name
from itops_reports.service import DefinitionInput, ReportsError

CONFIG_HELP = (
    "`config` is JSON: {filters: [{field, op, value}], window_days?, tiles: [{label, measure: "
    "{kind, field?}, filters?: [...]}], breakdowns: [{label, field, depth? (file paths: group "
    "by the first N parts), measure, limit?}], trend?: {label, interval, measure, split_by?, "
    "chart}, columns: [field keys for the list]}. `op` is one of "
    + ", ".join(OPS)
    + "; measure `kind` is one of "
    + ", ".join(MEASURES)
    + " (sum needs a number or money column; distinct counts different values); `interval` is "
    + ", ".join(INTERVALS)
    + "; `chart` is bar, stacked or line. Field keys come from "
    "reports_list_datasets."
)


@module.tool(permission="reports.read")
async def list_reports() -> list[dict[str, Any]]:
    """Published reports you can see: key, title, description, dataset and link."""
    async with module.session() as s:
        found = await service.definitions(s)
    return [
        {
            "key": d.key,
            "title": d.title,
            "description": d.description,
            "dataset": ds.key,
            "url": module.url(f"/r/{d.key}"),
        }
        for d, ds in found
    ]


@module.tool(permission="reports.read")
async def list_datasets() -> list[dict[str, Any]]:
    """Datasets you can see, with their columns (key, label, kind), where rows come from
    (synced by a connector or typed in), the time column and how long rows are kept. Money
    columns are not listed."""
    out: list[dict[str, Any]] = []
    async with module.session() as s:
        for ds in await service.datasets(s):
            cols = await service.columns(ds)
            out.append(
                {
                    "key": ds.key,
                    "title": ds.title,
                    "description": ds.description,
                    "mode": ds.mode,
                    "time_column": ds.time_field,
                    "retention_days": ds.retention_days,
                    "rows": await service.count(s, ds),
                    "columns": [
                        {"key": f.key, "label": f.label, "kind": f.kind} for f in cols.visible
                    ],
                }
            )
    return out


@module.tool(permission="reports.read")
async def get_report(key: str, days: int | None = None, search: str = "") -> dict[str, Any]:
    """A report's numbers: its tiles, breakdowns (top entries) and trend, over the last
    `days` (default: the report's own window) and optionally only rows matching `search`.
    Money is never included. Also the newest 20 rows of its list."""
    async with module.session() as s:
        found = await service.definition(s, key)
        if found is None or found[0].config is None:
            raise ReportsError(f"No report '{key}'.")
        d, ds = found
        cols = await service.columns(ds)
        cfg = await service.config_of(d, ds)
        window = days or cfg.window_days
        since = (
            dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=window)
            if window
            else None
        )
        items, truncated = await service.items(s, ds, since=since)
    result = evaluate(
        items,
        cfg,
        definition=cols.definition,
        hidden_fields=cols.hidden,
        query=search,
        since=since if ds.time_field else None,
    )
    keys = cfg.columns or [f.key for f in cols.visible]
    keys = [k for k in keys if k not in cols.hidden]
    return {
        "title": d.title,
        "url": module.url(f"/r/{d.key}"),
        "window_days": window,
        "rows": len(result.rows),
        "truncated": truncated,
        "tiles": [
            {"label": t["label"], "value": t["value"]}
            for t in result.tiles
            if t["value"] is not None
        ],
        "breakdowns": [
            {
                "label": b["label"],
                "measure": measure_name(b["measure"], cols.definition),
                "top": [
                    {"value": e["label"], "amount": e["value"]}
                    for e in b["entries"]
                    if e["value"] is not None
                ],
            }
            for b in result.breakdowns
        ],
        "trend": result.trend
        and {"labels": result.trend["labels"], "series": result.trend["series"]},
        "newest": [{k: it.values.get(k) for k in keys} for it in result.rows[:20]],
    }


@module.tool(
    permission="reports.write",
    writes=True,
    description=(
        "Draft a new report on a dataset (or draft changes to an existing report by its `key`). "
        "It's saved as a draft: a person with reports.publish checks and publishes it. "
        + CONFIG_HELP
    ),
)
async def draft_report(
    dataset: str, title: str, config: dict[str, Any], description: str = "", key: str | None = None
) -> dict[str, Any]:
    async with module.session() as s:
        existing = None
        if key:
            found = await service.definition(s, key)
            if found is None:
                raise ReportsError(f"No report '{key}'.")
            existing = found[0]
        d = await service.save_definition(
            s,
            DefinitionInput(
                title=title,
                dataset=dataset,
                config=config,
                description=description,
                key=None if existing else key,
            ),
            existing=existing,
        )
        await s.commit()
    return {
        "key": d.key,
        "status": "draft, waiting for a person to publish it",
        "url": module.url(f"/r/{d.key}?draft=1"),
    }
