"""What a report shows, and working it out (ADR 0016). No database here: rows in, numbers out.

A report definition (``ReportConfig``) is a dataset's rows narrowed by fixed filters, then:
summary tiles (a measure, optionally with its own filters), breakdowns (a measure grouped by
a column, or by the first N parts of a file path), a trend over time, and a detail list.
The viewer narrows further with a time window, a text search and "drill" filters (clicking a
breakdown entry).

Rows are small (a dataset holds what its retention keeps), so they're filtered and grouped
here in Python after one read, with a hard cap (``MAX_ROWS``).
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from itops.sdk.types import TypeDef

MAX_ROWS = 200_000
Op = Literal[
    "is",
    "is_not",
    "contains",
    "not_contains",
    "starts_with",
    "not_starts_with",
    "ends_with",
    "not_ends_with",
    "gte",
    "lte",
    "empty",
    "not_empty",
]
OPS: dict[str, str] = {
    "is": "is",
    "is_not": "is not",
    "contains": "contains",
    "not_contains": "doesn't contain",
    "starts_with": "starts with",
    "not_starts_with": "doesn't start with",
    "ends_with": "ends with",
    "not_ends_with": "doesn't end with",
    "gte": "is at least",
    "lte": "is at most",
    "empty": "is empty",
    "not_empty": "isn't empty",
}
MEASURES = {"rows": "Number of rows", "sum": "Sum of", "distinct": "Number of different"}
INTERVALS = {"day": "Day", "week": "Week", "month": "Month"}
NUMERIC = ("number", "money")
TIMED = ("date", "datetime")


class ReportProblem(ValueError):
    """A definition that doesn't fit its dataset; the message is for people (and AI)."""


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    op: Op = "is"
    value: str = ""


class Measure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["rows", "sum", "distinct"] = "rows"
    field: str | None = None


class Tile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=60)
    measure: Measure = Field(default_factory=Measure)
    filters: list[Filter] = Field(default_factory=list[Filter])


class Breakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    field: str
    depth: int | None = Field(
        default=None, ge=1, le=12, description="file paths: group by the first N parts"
    )
    measure: Measure = Field(default_factory=Measure)
    limit: int = Field(default=10, ge=1, le=50)


class Trend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="Over time", max_length=80)
    interval: Literal["day", "week", "month"] = "day"
    measure: Measure = Field(default_factory=Measure)
    split_by: str | None = None
    chart: Literal["bar", "stacked", "line"] = "bar"


class ReportConfig(BaseModel):
    """A report's definition, stored as JSON. Field names are the dataset type's keys."""

    model_config = ConfigDict(extra="forbid")
    filters: list[Filter] = Field(default_factory=list[Filter])
    window_days: int | None = Field(default=None, ge=1, le=3650)
    tiles: list[Tile] = Field(default_factory=list[Tile], max_length=8)
    breakdowns: list[Breakdown] = Field(default_factory=list[Breakdown], max_length=6)
    trend: Trend | None = None
    columns: list[str] = Field(default_factory=list[str])

    @field_validator("filters", "tiles", "breakdowns", "columns")
    @classmethod
    def _cap(cls, v: list[Any]) -> list[Any]:
        if len(v) > 30:
            raise ValueError("too many entries")
        return v


def parse_config(data: Any, definition: TypeDef, *, time_field: str | None) -> ReportConfig:
    """Validate a config against the dataset's columns; ReportProblem with readable text."""
    try:
        cfg = data if isinstance(data, ReportConfig) else ReportConfig.model_validate(data)
    except ValidationError as exc:
        problems = [f"{' '.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ReportProblem("Invalid report: " + "; ".join(problems)) from exc
    kinds = {f.key: f.kind for f in definition.fields}
    labels = {f.key: f.label for f in definition.fields}
    problems: list[str] = []

    def known(key: str | None, where: str) -> None:
        if key and key not in kinds:
            problems.append(f"{where}: no column '{key}' (columns: {', '.join(kinds)})")

    def check_measure(m: Measure, where: str) -> None:
        if m.kind == "rows":
            return
        if not m.field:
            problems.append(f"{where}: {m.kind} needs a column")
            return
        known(m.field, where)
        if m.kind == "sum" and m.field in kinds and kinds[m.field] not in NUMERIC:
            problems.append(
                f"{where}: can only sum a number or money column, not {labels[m.field]}"
            )

    for f in cfg.filters:
        known(f.field, "Filter")
    for t in cfg.tiles:
        check_measure(t.measure, f"Tile '{t.label}'")
        for f in t.filters:
            known(f.field, f"Tile '{t.label}' filter")
    for b in cfg.breakdowns:
        known(b.field, f"Breakdown '{b.label}'")
        check_measure(b.measure, f"Breakdown '{b.label}'")
    if cfg.trend:
        if not time_field:
            problems.append("A trend needs the dataset to have a time column")
        check_measure(cfg.trend.measure, "Trend")
        known(cfg.trend.split_by, "Trend split")
    for c in cfg.columns:
        known(c, "Detail columns")
    if problems:
        raise ReportProblem("; ".join(problems))
    return cfg


# ---- evaluating ---


def text_of(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        items: list[Any] = v  # pyright: ignore[reportUnknownVariableType]
        return ", ".join(text_of(i) for i in items)
    if isinstance(v, dict):
        d: dict[str, Any] = v  # pyright: ignore[reportUnknownVariableType]
        return str(d.get("id", ""))
    return str(v)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def matches(row: dict[str, Any], f: Filter) -> bool:
    raw = row.get(f.field)
    s, want = text_of(raw).lower(), f.value.strip().lower()
    match f.op:
        case "empty":
            return s == ""
        case "not_empty":
            return s != ""
        case "is":
            return s == want
        case "is_not":
            return s != want
        case "contains":
            return want in s
        case "not_contains":
            return want not in s
        case "starts_with":
            return s.startswith(want)
        case "not_starts_with":
            return not s.startswith(want)
        case "ends_with":
            return s.endswith(want)
        case "not_ends_with":
            return not s.endswith(want)
        case "gte" | "lte":
            a, b = _num(raw), _num(f.value)
            if a is None or b is None:  # dates compare as ISO text
                return (s >= want) if f.op == "gte" else (s <= want)
            return a >= b if f.op == "gte" else a <= b


def path_prefix(value: str, depth: int) -> str:
    """The first ``depth`` parts of a path (backslashes or slashes kept as they were)."""
    sep = "\\" if "\\" in value else "/"
    parts = [p for p in value.split(sep) if p]
    lead = sep * 2 if value.startswith(sep * 2) else (sep if value.startswith(sep) else "")
    return lead + sep.join(parts[:depth])


def measure(rows: Iterable[dict[str, Any]], m: Measure) -> float:
    rows = list(rows)
    if m.kind == "rows":
        return float(len(rows))
    if m.kind == "distinct":
        return float(len({text_of(r.get(m.field or "")) for r in rows} - {""}))
    return float(sum(_num(r.get(m.field or "")) or 0 for r in rows))


def bucket(moment: dt.datetime, interval: str) -> dt.date:
    d = moment.date()
    if interval == "week":
        return d - dt.timedelta(days=d.weekday())
    if interval == "month":
        return d.replace(day=1)
    return d


def bucket_label(d: dt.date, interval: str) -> str:
    if interval == "month":
        return d.strftime("%b %Y")
    if interval == "week":
        return f"Wk {d:%b} {d.day}"
    return f"{d:%b} {d.day}"


def _next(d: dt.date, interval: str) -> dt.date:
    if interval == "month":
        return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return d + dt.timedelta(days=7 if interval == "week" else 1)


@dataclass
class Item:
    """One row as the engine sees it: its values and its time (from the dataset's time column)."""

    id: int
    values: dict[str, Any]
    time: dt.datetime | None


@dataclass
class Result:
    rows: list[Item]
    tiles: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    breakdowns: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    trend: dict[str, Any] | None = None
    truncated: bool = False


def evaluate(
    items: Sequence[Item],
    cfg: ReportConfig,
    *,
    definition: TypeDef,
    hidden_fields: frozenset[str] = frozenset(),
    drill: Sequence[Filter] = (),
    query: str = "",
    since: dt.datetime | None = None,
) -> Result:
    """Narrow ``items`` (fixed filters, then the viewer's window, search and drill filters)
    and work out the tiles, breakdowns and trend. ``hidden_fields`` (money columns the
    viewer may not see) make any measure over them come back as None."""
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    keep: list[Item] = []
    for it in items:
        if since is not None and (it.time is None or it.time < since):
            continue
        if not all(matches(it.values, f) for f in [*cfg.filters, *drill]):
            continue
        if words:
            blob = " ".join(
                text_of(v) for k, v in it.values.items() if k not in hidden_fields
            ).lower()
            if not all(w in blob for w in words):
                continue
        keep.append(it)
    keep.sort(key=lambda it: (it.time or dt.datetime.min, it.id), reverse=True)
    result = Result(rows=keep)
    labels = {f.key: f.label for f in definition.fields}

    def value(rows: list[dict[str, Any]], m: Measure) -> float | None:
        return None if m.field in hidden_fields else measure(rows, m)

    values = [it.values for it in keep]
    for t in cfg.tiles:
        subset = [v for v in values if all(matches(v, f) for f in t.filters)]
        result.tiles.append(
            {"label": t.label, "value": value(subset, t.measure), "measure": t.measure}
        )
    for b in cfg.breakdowns:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for v in values:
            key = text_of(v.get(b.field))
            if b.depth:
                key = path_prefix(key, b.depth)
            groups[key or "(empty)"].append(v)
        entries = [
            {
                "label": k,
                "value": value(g, b.measure),
                "count": len(g),
                "drill": {
                    "field": b.field,
                    "op": "starts_with" if b.depth else "is",
                    "value": "" if k == "(empty)" else k,
                },
            }
            for k, g in groups.items()
        ]
        entries.sort(key=lambda e: (e["value"] or 0, e["count"]), reverse=True)
        result.breakdowns.append(
            {
                "label": b.label,
                "field": b.field,
                "column": labels.get(b.field, b.field),
                "measure": b.measure,
                "entries": entries[: b.limit],
                "more": max(len(entries) - b.limit, 0),
            }
        )
    if cfg.trend:
        t = cfg.trend
        timed = [it for it in keep if it.time is not None]
        if timed and t.measure.field not in hidden_fields:
            times = [it.time for it in timed if it.time is not None]
            start = bucket(since or min(times), t.interval)
            end = bucket(max(times), t.interval)
            buckets: list[dt.date] = []
            d = start
            while d <= end and len(buckets) < 400:
                buckets.append(d)
                d = _next(d, t.interval)
            series_keys = ["all"]
            if t.split_by:
                counts = Counter(text_of(it.values.get(t.split_by)) or "(empty)" for it in timed)
                series_keys = [k for k, _ in counts.most_common(5)]
            grid: dict[tuple[str, dt.date], list[dict[str, Any]]] = defaultdict(list)
            for it in timed:
                assert it.time is not None
                s = (text_of(it.values.get(t.split_by)) or "(empty)") if t.split_by else "all"
                if s not in series_keys:
                    s = "Other"
                grid[(s, bucket(it.time, t.interval))].append(it.values)
            if t.split_by and any(k[0] == "Other" for k in grid):
                series_keys.append("Other")
            result.trend = {
                "label": t.label,
                "chart": t.chart if t.split_by or t.chart != "stacked" else "bar",
                "labels": [bucket_label(b, t.interval) for b in buckets],
                "series": [
                    {
                        "name": s if t.split_by else _measure_name(t.measure, labels),
                        "values": [measure(grid.get((s, b), []), t.measure) for b in buckets],
                    }
                    for s in series_keys
                ],
                "money": t.measure.field is not None
                and _kind(definition, t.measure.field) == "money",
            }
    return result


def _kind(definition: TypeDef, key: str) -> str:
    return next((f.kind for f in definition.fields if f.key == key), "text")


def _measure_name(m: Measure, labels: dict[str, str]) -> str:
    if m.kind == "rows":
        return "Rows"
    return f"{MEASURES[m.kind]} {labels.get(m.field or '', m.field or '')}".strip()


def measure_name(m: Measure, definition: TypeDef) -> str:
    return _measure_name(m, {f.key: f.label for f in definition.fields})
