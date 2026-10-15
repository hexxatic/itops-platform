"""Lists as downloads: CSV or Excel (ADR 0016). One helper, so modules don't each write one.

Excel keeps types: dates are dates, money is a number with a currency format, and the
header row is frozen, bold and sized to the content. CSV is UTF-8 with a byte-order mark
(Excel then opens accents correctly). Text that a spreadsheet would run as a formula
(``=``, ``+``, ``-``, ``@`` at the start) is kept as text in both.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import quote

from starlette.responses import Response

from itops.core.charts import CURRENCY

ColumnKind = Literal["text", "number", "money", "date", "datetime"]
Format = Literal["csv", "xlsx"]
FORMATS: tuple[Format, ...] = ("csv", "xlsx")
MAX_ROWS = 1_000_000  # Excel's limit is 1,048,576 rows
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # not allowed in Excel cells


class ExportProblem(ValueError):
    """An export that can't be made (unknown format, too many rows)."""


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: ColumnKind = "text"


def _date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _datetime(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    try:
        return dt.datetime.fromisoformat(str(v).replace(" ", "T", 1)).replace(tzinfo=None)
    except ValueError:
        return None


def _number(v: Any) -> float | int | None:
    try:
        n = float(str(v).replace(",", "").replace(CURRENCY, "").strip())
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def _text(v: Any) -> str:
    if isinstance(v, Mapping):  # a link or table cell: {"text": ..., "href": ...}
        d = cast(Mapping[str, Any], v)
        v = d.get("text", d.get("label", ""))
    if isinstance(v, list | tuple):
        items = cast(Sequence[Any], v)
        return ", ".join(_text(i) for i in items)
    return _CONTROL.sub("", "" if v is None else str(v))


def cell_value(column: Column, raw: Any) -> Any:
    """The typed value for a cell (None for blank); text unchanged otherwise."""
    if raw is None or raw == "":
        return None
    if column.kind in ("number", "money"):
        n = _number(_text(raw) if isinstance(raw, Mapping) else raw)
        return n if n is not None else _text(raw)
    if column.kind == "date":
        return _date(raw) or _text(raw)
    if column.kind == "datetime":
        return _datetime(raw) or _text(raw)
    return _text(raw)


def safe_filename(name: str, fmt: Format) -> str:
    stem = re.sub(r"[^A-Za-z0-9 ._-]+", "", name).strip(" .") or "export"
    return f"{stem[:80]} {dt.date.today().isoformat()}.{fmt}"


def export_response(
    rows: Iterable[Mapping[str, Any]],
    columns: Sequence[Column],
    *,
    filename: str,
    format: str,
) -> Response:
    """``rows`` as a CSV or Excel download. ``format`` is "csv" or "xlsx" (anything else
    raises ExportProblem). Cells may be plain values or ``{"text": ...}`` table cells."""
    if format not in FORMATS:
        raise ExportProblem(f"Unknown format {format!r}: use csv or xlsx.")
    fmt: Format = format  # type: ignore[assignment]
    data = [[cell_value(c, row.get(c.key)) for c in columns] for row in rows]
    if len(data) > MAX_ROWS:
        raise ExportProblem(f"Too many rows to export ({len(data):,}); narrow the filters.")
    body = _xlsx(data, columns) if fmt == "xlsx" else _csv(data, columns)
    name = safe_filename(filename, fmt)
    return Response(
        body,
        media_type=XLSX_TYPE if fmt == "xlsx" else "text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _csv(data: list[list[Any]], columns: Sequence[Column]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow([c.label for c in columns])
    for row in data:
        cells: list[str] = []
        for c, v in zip(columns, row, strict=True):
            if v is None:
                cells.append("")
            elif isinstance(v, dt.datetime):
                cells.append(v.isoformat(sep=" ", timespec="minutes"))
            elif isinstance(v, dt.date):
                cells.append(v.isoformat())
            elif c.kind == "money" and isinstance(v, int | float):
                cells.append(f"{v:.2f}")
            elif isinstance(v, str) and v.startswith(_FORMULA_START):
                cells.append("'" + v)
            else:
                cells.append(str(v))
        w.writerow(cells)
    return ("﻿" + out.getvalue()).encode("utf-8")


def _xlsx(data: list[list[Any]], columns: Sequence[Column]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    formats = {
        "money": f'"{CURRENCY}"#,##0.00;-"{CURRENCY}"#,##0.00',
        "number": "#,##0.##",
        "date": "yyyy-mm-dd",
        "datetime": "yyyy-mm-dd hh:mm",
    }
    wb = Workbook(write_only=True)
    ws: Any = wb.create_sheet("Export")
    for i, c in enumerate(columns, start=1):
        longest = max([len(c.label), *(len(_shown(v)) for v in (r[i - 1] for r in data[:2000]))])
        ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 8), 60)
    ws.freeze_panes = "A2"
    header: list[Any] = []
    for c in columns:
        cell = WriteOnlyCell(ws, value=c.label)
        cell.font = Font(bold=True)
        header.append(cell)
    ws.append(header)
    for row in data:
        cells: list[Any] = []
        for c, v in zip(columns, row, strict=True):
            cell = WriteOnlyCell(ws, value=v)
            if isinstance(v, str):
                cell.data_type = "s"  # never a formula, whatever it starts with
            elif v is not None and c.kind in formats:
                cell.number_format = formats[c.kind]
            cells.append(cell)
        ws.append(cells)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _shown(v: Any) -> str:
    if isinstance(v, dt.datetime):
        return "yyyy-mm-dd hh:mm"
    if isinstance(v, dt.date):
        return "yyyy-mm-dd"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return "" if v is None else str(v)
