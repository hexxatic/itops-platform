"""Reports pages (server-rendered, core components only) and the form actions behind them.

Pages live at /reports/...; actions are POSTs under /api/reports/... that redirect back to a
page. Problems come back as ``?error=...`` and a failed save keeps the form.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from itops.sdk import KeptForm, Principal
from itops.sdk.export import Column, ExportProblem, export_response, money, number
from itops_reports import service
from itops_reports.app import module
from itops_reports.engine import (
    INTERVALS,
    MEASURES,
    NUMERIC,
    OPS,
    Filter,
    ReportConfig,
    evaluate,
    measure_name,
    text_of,
)
from itops_reports.models import Dataset, Definition
from itops_reports.service import MODES, DatasetInput, DefinitionInput, ReportsError

CanRead = Annotated[Principal, Depends(module.require("reports.read"))]
CanWrite = Annotated[Principal, Depends(module.require("reports.write"))]
CanPublish = Annotated[Principal, Depends(module.require("reports.publish"))]
WINDOWS = [
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
    ("365", "Last year"),
    ("all", "Everything kept"),
]
SPARE = {"filters": 4, "tiles": 6, "breakdowns": 4}
EXPORT_KINDS = {"number": "number", "money": "money", "date": "date", "datetime": "datetime"}


def _can(permission: str) -> bool:
    p = module.current_principal()
    return bool(p and p.has(permission))


def _back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


def display(kind: str, value: Any) -> str:
    """A stored value as shown in tables."""
    if value is None or value == "" or value == []:
        return ""
    if kind == "money":
        return money(value)
    if kind == "number":
        return number(value)
    if kind == "datetime":
        return str(value).replace("T", " ")
    return text_of(value)


def _shown(value: float | None, kind: str) -> str:
    if value is None:
        return "—"
    return money(value) if kind == "money" else number(value)


async def _dataset_or_404(s: Any, key: str) -> Dataset:
    ds = await service.dataset(s, key)
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


async def _report_or_404(s: Any, key: str) -> tuple[Definition, Dataset]:
    found = await service.definition(s, key)
    if found is None or (found[0].config is None and not _can("reports.publish")):
        raise HTTPException(status_code=404, detail="Report not found")
    return found


# ---- reports ---


@module.page("/", template="index.html", title="Reports", nav="Reports", permission="reports.read")
async def home(request: Request) -> dict[str, Any]:
    publisher = _can("reports.publish")
    async with module.session() as s:
        found = await service.definitions(s, drafts=publisher)
    published = [(d, ds) for d, ds in found if d.config is not None]
    drafts = [(d, ds) for d, ds in found if d.draft_config]
    return {
        "reports": [
            {
                "title": d.title,
                "href": f"/reports/r/{d.key}",
                "description": d.description,
                "dataset": ds.title,
            }
            for d, ds in published
        ],
        "drafts": [
            {
                "report": {"text": d.title, "href": f"/reports/r/{d.key}?draft=1"},
                "by": d.draft_by or "",
                "state": {"badge": "New" if d.config is None else "Changes", "tone": "info"},
            }
            for d, _ in drafts
        ],
        "can_publish": publisher,
        "error": request.query_params.get("error"),
    }


def _drills(request: Request) -> list[Filter]:
    out: list[Filter] = []
    for raw in request.query_params.getlist("w")[:6]:
        field, _, rest = raw.partition("|")
        op, _, value = rest.partition("|")
        if field and op in ("is", "starts_with"):
            out.append(Filter(field=field, op=op, value=value))  # type: ignore[arg-type]
    return out


def _since(request: Request, cfg: ReportConfig) -> tuple[str, dt.datetime | None]:
    days = request.query_params.get("days") or (str(cfg.window_days) if cfg.window_days else "all")
    if days == "all" or not days.isdigit():
        return "all", None
    return days, dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=int(days))


def _url(base: str, params: list[tuple[str, str]]) -> str:
    return base + ("?" + urlencode(params) if params else "")


async def _evaluate(request: Request, key: str) -> dict[str, Any]:
    draft = request.query_params.get("draft") == "1" and _can("reports.publish")
    async with module.session() as s:
        d, ds = await _report_or_404(s, key)
        cols = await service.columns(ds)
        try:
            cfg = await service.config_of(d, ds, draft=draft or d.config is None)
        except (ReportsError, ValueError) as exc:
            raise HTTPException(
                status_code=409, detail=f"This report no longer fits its dataset: {exc}"
            ) from exc
        days, since = _since(request, cfg)
        found, truncated = await service.items(s, ds, since=since)
    drills, query = _drills(request), (request.query_params.get("q") or "").strip()
    result = evaluate(
        found,
        cfg,
        definition=cols.definition,
        hidden_fields=cols.hidden,
        drill=drills,
        query=query,
        since=since if ds.time_field else None,
    )
    result.truncated = truncated
    return {
        "d": d,
        "ds": ds,
        "cols": cols,
        "cfg": cfg,
        "days": days,
        "since": since,
        "drills": drills,
        "query": query,
        "result": result,
        "draft": draft or d.config is None,
    }


def _detail_columns(cfg: ReportConfig, cols: service.Columns) -> list[Any]:
    wanted = cfg.columns or [f.key for f in cols.definition.fields]
    by_key = {f.key: f for f in cols.visible}
    return [by_key[k] for k in wanted if k in by_key]


@module.page("/r/{key}", template="report.html", title="Report", permission="reports.read")
async def report_page(request: Request) -> dict[str, Any]:
    ctx = await _evaluate(request, request.path_params["key"])
    d, ds, cols, cfg, result = ctx["d"], ctx["ds"], ctx["cols"], ctx["cfg"], ctx["result"]
    kinds = {f.key: f.kind for f in cols.definition.fields}
    base = f"/reports/r/{d.key}"
    keep: list[tuple[str, str]] = []
    if ctx["draft"] and d.config is not None:
        keep.append(("draft", "1"))
    if request.query_params.get("days"):
        keep.append(("days", ctx["days"]))
    if ctx["query"]:
        keep.append(("q", ctx["query"]))
    drill_params = [("w", f"{f.field}|{f.op}|{f.value}") for f in ctx["drills"]]

    def kind_of(m: Any) -> str:
        return kinds.get(m.field or "", "number") if m.kind == "sum" else "number"

    tiles = [
        {
            "label": t["label"],
            "value": _shown(t["value"], kind_of(t["measure"])),
            "basis": None if t["value"] is not None else "estimate",
            "basis_note": None if t["value"] is not None else "Money columns need reports.costs",
        }
        for t in result.tiles
    ]
    breakdowns: list[dict[str, Any]] = []
    for b in result.breakdowns:
        k = kind_of(b["measure"])
        rows: list[dict[str, Any]] = []
        for e in b["entries"]:
            drill = e["drill"]
            href = _url(
                base,
                [*keep, *drill_params, ("w", f"{drill['field']}|{drill['op']}|{drill['value']}")],
            )
            rows.append(
                {
                    "label": e["label"],
                    "value": e["value"] or 0,
                    "display": _shown(e["value"], k),
                    "href": href,
                }
            )
        breakdowns.append(
            {
                "label": b["label"],
                "rows": rows,
                "more": b["more"],
                "measure": measure_name(b["measure"], cols.definition),
            }
        )
    detail_cols = _detail_columns(cfg, cols)
    detail = [
        {f.key: display(f.kind, it.values.get(f.key)) for f in detail_cols}
        for it in result.rows[:100]
    ]
    drill_labels = [
        {
            "text": f"{_label(cols, f.field)} {OPS[f.op]} “{f.value or '(empty)'}”",
            "href": _url(
                base, [*keep, *[p for p in drill_params if p[1] != f"{f.field}|{f.op}|{f.value}"]]
            ),
        }
        for f in ctx["drills"]
    ]
    access = await module.areas.access("report", d.key) if _can("reports.publish") else None
    return {
        "page_title": d.title,
        "d": d,
        "ds": ds,
        "tiles": tiles,
        "breakdowns": breakdowns,
        "trend": result.trend,
        "detail_columns": [(f.key, f.label) for f in detail_cols],
        "detail": detail,
        "shown": len(result.rows),
        "truncated": result.truncated,
        "windows": WINDOWS if ds.time_field else [],
        "days": ctx["days"],
        "query": ctx["query"],
        "drills": drill_labels,
        "drill_params": drill_params,
        "export_url": _url(f"/api/reports/r/{d.key}/export", [*keep, *drill_params]),
        "clear_url": base + ("?draft=1" if ctx["draft"] and d.config is not None else ""),
        "draft": ctx["draft"],
        "has_draft": bool(d.draft_config),
        "draft_by": d.draft_by,
        "can_publish": _can("reports.publish"),
        "access": access,
        "error": request.query_params.get("error"),
        "filters_text": [
            f"{_label(cols, f.field)} {OPS[f.op]} “{f.value}”".replace(" “”", "")
            for f in cfg.filters
        ],
    }


def _label(cols: service.Columns, key: str) -> str:
    return next((f.label for f in cols.definition.fields if f.key == key), key)


@module.router.get("/r/{key}/export")
async def export_report(
    request: Request, key: str, user: CanRead, format: str = "xlsx"
) -> Response:
    ctx = await _evaluate(request, key)
    cols: service.Columns = ctx["cols"]
    detail_cols = _detail_columns(ctx["cfg"], cols)
    rows = [{f.key: it.values.get(f.key) for f in detail_cols} for it in ctx["result"].rows]
    columns = [Column(f.key, f.label, EXPORT_KINDS.get(f.kind, "text")) for f in detail_cols]  # type: ignore[arg-type]
    try:
        return export_response(rows, columns, filename=ctx["d"].title, format=format)
    except ExportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- report editor ---


def _pad(items: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    return items + [{} for _ in range(n)]


async def _editor(
    request: Request, d: Definition | None, ds: Dataset, cfg: ReportConfig | None
) -> dict[str, Any]:
    kept = module.forms.recall(request)
    cols = await service.columns(ds)
    fields = [("", "—")] + [(f.key, f.label) for f in cols.definition.fields]
    numeric = [("", "—")] + [(f.key, f.label) for f in cols.definition.fields if f.kind in NUMERIC]
    cfg = cfg or ReportConfig()
    filters = [f.model_dump() for f in cfg.filters]
    tiles = [
        {
            "label": t.label,
            "kind": t.measure.kind,
            "field": t.measure.field or "",
            **(
                {"ffield": t.filters[0].field, "fop": t.filters[0].op, "fvalue": t.filters[0].value}
                if t.filters
                else {}
            ),
        }
        for t in cfg.tiles
    ]
    breakdowns = [
        {
            "label": b.label,
            "field": b.field,
            "depth": b.depth or "",
            "kind": b.measure.kind,
            "mfield": b.measure.field or "",
            "limit": b.limit,
        }
        for b in cfg.breakdowns
    ]
    trend = cfg.trend
    return {
        "d": d,
        "ds": ds,
        "kept": kept is not None,
        "k": kept,
        "title": kept.get("title", "") if kept else (d.title if d else ""),
        "description": kept.get("description", "") if kept else (d.description if d else ""),
        "window_days": kept.get("window_days", "") if kept else (cfg.window_days or ""),
        "fields": fields,
        "numeric": numeric,
        "ops": list(OPS.items()),
        "measures": list(MEASURES.items()),
        "intervals": list(INTERVALS.items()),
        "charts": [("bar", "Bars"), ("stacked", "Stacked bars"), ("line", "Line")],
        "filters": _pad(filters, SPARE["filters"]),
        "tiles": _pad(tiles, SPARE["tiles"]),
        "breakdowns": _pad(breakdowns, SPARE["breakdowns"]),
        "trend": {
            "on": trend is not None,
            "label": trend.label if trend else "Over time",
            "interval": trend.interval if trend else "day",
            "kind": trend.measure.kind if trend else "rows",
            "field": (trend.measure.field or "") if trend else "",
            "split": (trend.split_by or "") if trend else "",
            "chart": trend.chart if trend else "bar",
        },
        "columns": [(f.key, f.label, f.key in cfg.columns) for f in cols.definition.fields],
        "has_time": bool(ds.time_field),
        "error": request.query_params.get("error"),
        "action": f"/api/reports/r/{d.key}" if d else "/api/reports/r",
    }


@module.page("/new", template="report_edit.html", title="New report", permission="reports.publish")
async def new_report(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        key = request.query_params.get("dataset", "")
        ds = await service.dataset(s, key) if key else None
        if ds is None:
            choices = await service.datasets(s)
            return {
                "choose": [
                    {
                        "text": c.title,
                        "href": f"/reports/new?dataset={c.key}",
                        "note": c.description,
                    }
                    for c in choices
                ],
                "error": request.query_params.get("error"),
            }
    return {"choose": None, **(await _editor(request, None, ds, None))}


@module.page(
    "/r/{key}/edit", template="report_edit.html", title="Edit report", permission="reports.publish"
)
async def edit_report(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        d, ds = await _report_or_404(s, request.path_params["key"])
        cfg = await service.config_of(d, ds, draft=True)
    return {"choose": None, **(await _editor(request, d, ds, cfg))}


def _measure(kind: str, field: str) -> dict[str, Any]:
    return (
        {"kind": kind or "rows", "field": field or None}
        if kind and kind != "rows"
        else {"kind": "rows"}
    )


def config_from_form(form: Any) -> dict[str, Any]:
    """The editor's form fields -> a ReportConfig dict (blank rows dropped)."""

    def g(name: str) -> str:
        return str(form.get(name) or "").strip()

    filters = [
        {"field": g(f"flt_field_{i}"), "op": g(f"flt_op_{i}") or "is", "value": g(f"flt_value_{i}")}
        for i in range(30)
        if g(f"flt_field_{i}")
    ]
    tiles: list[dict[str, Any]] = []
    for i in range(30):
        if not g(f"tile_label_{i}"):
            continue
        tile: dict[str, Any] = {
            "label": g(f"tile_label_{i}"),
            "measure": _measure(g(f"tile_kind_{i}"), g(f"tile_field_{i}")),
        }
        if g(f"tile_ffield_{i}"):
            tile["filters"] = [
                {
                    "field": g(f"tile_ffield_{i}"),
                    "op": g(f"tile_fop_{i}") or "is",
                    "value": g(f"tile_fvalue_{i}"),
                }
            ]
        tiles.append(tile)
    breakdowns: list[dict[str, Any]] = []
    for i in range(30):
        if not g(f"bd_field_{i}"):
            continue
        b: dict[str, Any] = {
            "label": g(f"bd_label_{i}") or g(f"bd_field_{i}"),
            "field": g(f"bd_field_{i}"),
            "measure": _measure(g(f"bd_kind_{i}"), g(f"bd_mfield_{i}")),
        }
        if g(f"bd_depth_{i}").isdigit():
            b["depth"] = int(g(f"bd_depth_{i}"))
        if g(f"bd_limit_{i}").isdigit():
            b["limit"] = int(g(f"bd_limit_{i}"))
        breakdowns.append(b)
    out: dict[str, Any] = {
        "filters": filters,
        "tiles": tiles,
        "breakdowns": breakdowns,
        "columns": [str(c) for c in form.getlist("columns")],
    }
    if g("window_days").isdigit():
        out["window_days"] = int(g("window_days"))
    if g("tr_on"):
        out["trend"] = {
            "label": g("tr_label") or "Over time",
            "interval": g("tr_interval") or "day",
            "measure": _measure(g("tr_kind"), g("tr_field")),
            "split_by": g("tr_split") or None,
            "chart": g("tr_chart") or "bar",
        }
    return out


@module.router.post("/r")
async def create_report(request: Request, user: CanPublish) -> RedirectResponse:
    form = await request.form()
    dataset = str(form.get("dataset") or "")
    try:
        async with module.session() as s:
            d = await service.save_definition(
                s,
                DefinitionInput(
                    title=str(form.get("title") or ""),
                    description=str(form.get("description") or ""),
                    dataset=dataset,
                    config=config_from_form(form),
                ),
            )
            await s.commit()
    except (ReportsError, ValueError) as exc:
        return await module.forms.back(request, f"/reports/new?dataset={quote(dataset)}", str(exc))
    return RedirectResponse(f"/reports/r/{d.key}", status_code=303)


@module.router.post("/r/{key}")
async def update_report(request: Request, key: str, user: CanPublish) -> RedirectResponse:
    form = await request.form()
    try:
        async with module.session() as s:
            d, ds = await _report_or_404(s, key)
            await service.save_definition(
                s,
                DefinitionInput(
                    title=str(form.get("title") or ""),
                    description=str(form.get("description") or ""),
                    dataset=ds.key,
                    config=config_from_form(form),
                ),
                existing=d,
            )
            await s.commit()
    except (ReportsError, ValueError) as exc:
        return await module.forms.back(request, f"/reports/r/{key}/edit", str(exc))
    return RedirectResponse(f"/reports/r/{key}", status_code=303)


@module.router.post("/r/{key}/publish")
async def publish_report(key: str, user: CanPublish) -> RedirectResponse:
    try:
        async with module.session() as s:
            d, ds = await _report_or_404(s, key)
            await service.publish_draft(s, d, ds)
            await s.commit()
    except (ReportsError, ValueError) as exc:
        return _back(f"/reports/r/{key}?draft=1", str(exc))
    return _back(f"/reports/r/{key}")


@module.router.post("/r/{key}/discard")
async def discard_report(key: str, user: CanPublish) -> RedirectResponse:
    async with module.session() as s:
        d, _ = await _report_or_404(s, key)
        gone = await service.discard_draft(s, d)
        await s.commit()
    return _back("/reports/" if gone else f"/reports/r/{key}")


@module.router.post("/r/{key}/delete")
async def delete_report(key: str, user: CanPublish) -> RedirectResponse:
    async with module.session() as s:
        d, _ = await _report_or_404(s, key)
        await service.delete_definition(s, d)
        await s.commit()
    return _back("/reports/")


# ---- datasets ---


@module.page("/datasets", template="datasets.html", title="Datasets", permission="reports.read")
async def datasets_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        found = await service.datasets(s)
        counts = await service.row_counts(s)
    return {
        "datasets": [
            {
                "title": {"text": d.title, "href": f"/reports/d/{d.key}"},
                "mode": MODES[d.mode],
                "rows": number(counts.get(d.id, 0)),
                "kept": f"{d.retention_days} days" if d.retention_days else "Everything",
            }
            for d in found
        ],
        "can_publish": _can("reports.publish"),
        "error": request.query_params.get("error"),
    }


@module.page("/d/{key}", template="dataset.html", title="Dataset", permission="reports.read")
async def dataset_page(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        ds = await _dataset_or_404(s, request.path_params["key"])
        cols = await service.columns(ds)
        page = await service.rows_page(s, ds)
        total = await service.count(s, ds)
    editable = ds.mode == "manual" and _can("reports.write")
    columns = [(f.key, f.label) for f in cols.visible]
    rows: list[dict[str, Any]] = []
    for r, values in page:
        row: dict[str, Any] = {f.key: display(f.kind, values.get(f.key)) for f in cols.visible}
        if editable:
            row["_edit"] = {"text": "Edit", "href": f"/reports/d/{ds.key}/rows/{r.id}"}
        rows.append(row)
    if editable:
        columns.append(("_edit", ""))
    access = await module.areas.access("dataset", ds.key) if _can("reports.publish") else None
    return {
        "page_title": ds.title,
        "ds": ds,
        "mode": MODES[ds.mode],
        "columns": columns,
        "rows": rows,
        "total": total,
        "editable": editable,
        "can_publish": _can("reports.publish"),
        "hidden_money": bool(cols.hidden),
        "time_label": _label(cols, ds.time_field) if ds.time_field else None,
        "access": access,
        "history": await module.audit.history(kind="dataset", entity_id=ds.key, limit=20),
        "error": request.query_params.get("error"),
        "export_url": f"/api/reports/d/{ds.key}/export",
    }


@module.router.get("/d/{key}/export")
async def export_dataset(key: str, user: CanRead, format: str = "xlsx") -> Response:
    async with module.session() as s:
        ds = await _dataset_or_404(s, key)
        cols = await service.columns(ds)
        found, _ = await service.items(s, ds)
    columns = [Column(f.key, f.label, EXPORT_KINDS.get(f.kind, "text")) for f in cols.visible]  # type: ignore[arg-type]
    try:
        return export_response(
            [it.values for it in found], columns, filename=ds.title, format=format
        )
    except ExportProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _dataset_form(request: Request, ds: Dataset | None) -> dict[str, Any]:
    kept = module.forms.recall(request)
    types = await module.types.list()

    def pick(name: str, fallback: Any) -> Any:
        return kept.get(name, "") if kept else fallback

    time_fields: list[tuple[str, str]] = [("", "None")]
    for t in types:
        if t.published:
            time_fields += [
                (f.key, f"{t.published.definition.name}: {f.label}")
                for f in t.published.definition.fields
                if f.kind in ("date", "datetime")
            ]
    return {
        "ds": ds,
        "types": [(t.key, t.published.definition.name) for t in types if t.published],
        "modes": list(MODES.items()),
        "time_fields": time_fields,
        "title": pick("title", ds.title if ds else ""),
        "description": pick("description", ds.description if ds else ""),
        "type_key": pick("type_key", ds.type_key if ds else ""),
        "mode": pick("mode", ds.mode if ds else "manual"),
        "time_field": pick("time_field", (ds.time_field or "") if ds else ""),
        "retention_days": pick("retention_days", (ds.retention_days or "") if ds else ""),
        "error": request.query_params.get("error"),
        "action": f"/api/reports/d/{ds.key}" if ds else "/api/reports/d",
    }


@module.page(
    "/datasets/new", template="dataset_edit.html", title="New dataset", permission="reports.publish"
)
async def new_dataset(request: Request) -> dict[str, Any]:
    return await _dataset_form(request, None)


@module.page(
    "/d/{key}/settings",
    template="dataset_edit.html",
    title="Dataset settings",
    permission="reports.publish",
)
async def dataset_settings(request: Request) -> dict[str, Any]:
    async with module.session() as s:
        ds = await _dataset_or_404(s, request.path_params["key"])
    return await _dataset_form(request, ds)


def _dataset_input(form: Any) -> DatasetInput:
    return DatasetInput(
        title=str(form.get("title") or ""),
        type_key=str(form.get("type_key") or ""),
        mode=str(form.get("mode") or "manual"),
        description=str(form.get("description") or ""),
        time_field=str(form.get("time_field") or "") or None,
        retention_days=str(form.get("retention_days") or "") or None,
    )


@module.router.post("/d")
async def create_dataset(request: Request, user: CanPublish) -> RedirectResponse:
    form = await request.form()
    try:
        async with module.session() as s:
            ds = await service.create_dataset(s, _dataset_input(form))
            await s.commit()
    except ReportsError as exc:
        return await module.forms.back(request, "/reports/datasets/new", str(exc))
    return RedirectResponse(f"/reports/d/{ds.key}", status_code=303)


@module.router.post("/d/{key}")
async def update_dataset(request: Request, key: str, user: CanPublish) -> RedirectResponse:
    form = await request.form()
    try:
        async with module.session() as s:
            ds = await _dataset_or_404(s, key)
            await service.update_dataset(s, ds, _dataset_input(form))
            await s.commit()
    except ReportsError as exc:
        return await module.forms.back(request, f"/reports/d/{key}/settings", str(exc))
    return RedirectResponse(f"/reports/d/{key}", status_code=303)


# ---- rows ---


async def _row_form(request: Request, row_id: int | None) -> dict[str, Any]:
    kept: KeptForm | None = module.forms.recall(request)
    async with module.session() as s:
        ds = await _dataset_or_404(s, request.path_params["key"])
        if ds.mode != "manual":
            raise HTTPException(status_code=404, detail="A connector writes this dataset")
        cols = await service.columns(ds)
        values: dict[str, Any] = {}
        if row_id is not None:
            r = await service.get_row(s, ds, row_id)
            if r is None:
                raise HTTPException(status_code=404, detail="Row not found")
            values = {k: v for k, v in json.loads(r.values).items() if k not in cols.hidden}
    visible = cols.definition.model_copy(update={"fields": cols.visible})
    return {
        "ds": ds,
        "definition": visible,
        "values": kept.fields() if kept else values,
        "row_id": row_id,
        "hidden_money": bool(cols.hidden),
        "error": request.query_params.get("error"),
        "action": f"/api/reports/d/{ds.key}/rows" + (f"/{row_id}" if row_id else ""),
    }


@module.page(
    "/d/{key}/rows/new", template="row_edit.html", title="Add a row", permission="reports.write"
)
async def new_row(request: Request) -> dict[str, Any]:
    return await _row_form(request, None)


@module.page(
    "/d/{key}/rows/{row_id}", template="row_edit.html", title="Edit row", permission="reports.write"
)
async def edit_row(request: Request) -> dict[str, Any]:
    return await _row_form(request, int(request.path_params["row_id"]))


async def _form_values(request: Request, ds: Dataset) -> dict[str, Any]:
    form = await request.form()
    cols = await service.columns(ds)
    out: dict[str, Any] = {}
    for f in cols.visible:
        got = [str(v) for v in form.getlist(f"f__{f.key}") if str(v).strip()]
        out[f.key] = got if f.multiple else (got[0] if got else "")
    return out


@module.router.post("/d/{key}/rows")
async def add_row(request: Request, key: str, user: CanWrite) -> RedirectResponse:
    try:
        async with module.session() as s:
            ds = await _dataset_or_404(s, key)
            await service.add_row(s, ds, await _form_values(request, ds))
            await s.commit()
    except ReportsError as exc:
        return await module.forms.back(request, f"/reports/d/{key}/rows/new", str(exc))
    if (await request.form()).get("again"):
        return _back(f"/reports/d/{key}/rows/new")
    return _back(f"/reports/d/{key}")


@module.router.post("/d/{key}/rows/{row_id}")
async def update_row(request: Request, key: str, row_id: int, user: CanWrite) -> RedirectResponse:
    try:
        async with module.session() as s:
            ds = await _dataset_or_404(s, key)
            r = await service.get_row(s, ds, row_id)
            if r is None:
                raise HTTPException(status_code=404, detail="Row not found")
            await service.update_row(s, ds, r, await _form_values(request, ds))
            await s.commit()
    except ReportsError as exc:
        return await module.forms.back(request, f"/reports/d/{key}/rows/{row_id}", str(exc))
    return _back(f"/reports/d/{key}")


@module.router.post("/d/{key}/rows/{row_id}/delete")
async def delete_row(key: str, row_id: int, user: CanWrite) -> RedirectResponse:
    try:
        async with module.session() as s:
            ds = await _dataset_or_404(s, key)
            r = await service.get_row(s, ds, row_id)
            if r is None:
                raise HTTPException(status_code=404, detail="Row not found")
            await service.delete_row(s, ds, r)
            await s.commit()
    except ReportsError as exc:
        return _back(f"/reports/d/{key}", str(exc))
    return _back(f"/reports/d/{key}")


@module.page(
    "/d/{key}/paste", template="paste.html", title="Paste rows", permission="reports.write"
)
async def paste_page(request: Request) -> dict[str, Any]:
    kept = module.forms.recall(request)
    async with module.session() as s:
        ds = await _dataset_or_404(s, request.path_params["key"])
        if ds.mode != "manual":
            raise HTTPException(status_code=404, detail="A connector writes this dataset")
        cols = await service.columns(ds)
    text = kept.get("text", "") if kept else ""
    preview: dict[str, Any] | None = None
    if text and request.query_params.get("preview"):
        try:
            pasted = await service.read_paste(ds, text)
            kinds = {f.key: f.kind for f in cols.definition.fields}
            labels = {f.key: f.label for f in cols.definition.fields}
            preview = {
                "columns": [(k, labels[k]) for k in pasted.columns],
                "rows": [
                    {k: display(kinds[k], r.get(k)) for k in pasted.columns}
                    for r in pasted.rows[:50]
                ],
                "count": len(pasted.rows),
                "problems": pasted.problems,
            }
        except ReportsError as exc:
            preview = {"columns": [], "rows": [], "count": 0, "problems": [str(exc)]}
    return {
        "ds": ds,
        "text": text,
        "preview": preview,
        "labels": [f.label for f in cols.visible],
        "error": request.query_params.get("error"),
    }


@module.router.post("/d/{key}/paste")
async def paste_rows(
    request: Request,
    key: str,
    user: CanWrite,
    text: Annotated[str, Form()] = "",
    step: Annotated[str, Form()] = "preview",
) -> RedirectResponse:
    if step != "save":
        resp = await module.forms.back(request, f"/reports/d/{key}/paste?preview=1")
        return resp
    try:
        async with module.session() as s:
            ds = await _dataset_or_404(s, key)
            n = await service.save_paste(s, ds, text)
            await s.commit()
    except ReportsError as exc:
        return await module.forms.back(request, f"/reports/d/{key}/paste?preview=1", str(exc))
    return _back(f"/reports/d/{key}?added={n}")
