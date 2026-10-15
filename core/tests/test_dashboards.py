"""Dashboard components (ADR 0016): chart geometry, number formats, CSV/Excel export, the
money and datetime field kinds, and the macros rendering."""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from openpyxl import load_workbook

from itops.core.charts import chart_layout, compact, money, nice_ticks, number
from itops.core.export import Column, ExportProblem, export_response
from itops.core.types import FieldDef, TypeDef, TypeProblem, validate_values

# ---- formats and axis ----------------------------------------------------------------


def test_number_formats() -> None:
    assert number(1234.5) == "1,234.50"
    assert number(1234) == "1,234"
    assert money(1234.5) == "$1,234.50"
    assert money(-12) == "-$12.00"
    assert money(None) == "" and number("") == ""
    assert compact(950) == "950"
    assert compact(1200) == "1.2k"
    assert compact(3_400_000, is_money=True) == "$3.4M"
    assert compact(-2000) == "-2k"


def test_nice_ticks_are_round_and_include_zero() -> None:
    assert nice_ticks(0, 97) == [0, 25, 50, 75, 100]
    assert nice_ticks(12, 18)[0] == 0  # bars start at zero
    ticks = nice_ticks(-30, 70)
    assert 0 in ticks and ticks[0] <= -30 and ticks[-1] >= 70
    assert nice_ticks(0, 0) == [0, 0.25, 0.5, 0.75, 1]


# ---- chart geometry --------------------------------------------------------------------


def test_bar_chart_geometry() -> None:
    c = chart_layout(
        "bar",
        ["2024", "2025"],
        [{"name": "Laptops", "values": [10, 20]}, {"name": "Desktops", "values": [5, None]}],
    )
    assert len(c["bars"]) == 3  # the gap draws nothing
    tall = max(c["bars"], key=lambda b: b["h"])
    assert tall["title"] == "Laptops · 2025: 20"
    assert tall["y"] >= c["plot"]["y"] and tall["y"] + tall["h"] == pytest.approx(c["zero"])
    assert {b["series"] for b in c["bars"]} == {1, 2}
    assert c["table"]["rows"][1]["cells"] == ["5", ""]


def test_stacked_bars_sit_on_each_other() -> None:
    c = chart_layout(
        "stacked",
        ["Q1"],
        [{"name": "A", "values": [10]}, {"name": "B", "values": [5]}],
        is_money=True,
    )
    a, b = c["bars"]
    assert a["y"] == pytest.approx(b["y"] + b["h"])  # B starts where A ends
    assert a["title"].endswith("$10.00")
    assert c["y_ticks"][-1]["text"].startswith("$")


def test_line_chart_breaks_at_gaps() -> None:
    c = chart_layout(
        "line", ["Jan", "Feb", "Mar", "Apr"], [{"name": "Seats", "values": [3, None, 5, 6]}]
    )
    d = c["lines"][0]["d"]
    assert d.count("M") == 2 and d.count("L") == 1
    assert len(c["lines"][0]["points"]) == 3


def test_many_categories_thin_the_labels() -> None:
    labels = [f"D{i}" for i in range(60)]
    c = chart_layout("bar", labels, [{"name": "x", "values": list(range(60))}])
    assert len(c["x_labels"]) <= 12 and c["x_labels"][0]["text"] == "D0"


# ---- export ----------------------------------------------------------------------------

COLUMNS = [
    Column("name", "Name"),
    Column("cost", "Cost", "money"),
    Column("ends", "Ends", "date"),
    Column("at", "At", "datetime"),
    Column("n", "Count", "number"),
]
ROWS = [
    {
        "name": {"text": "Laptop", "href": "/x"},
        "cost": "1,250.5",
        "ends": "2027-01-31",
        "at": "2026-10-14T13:05",
        "n": 3,
    },
    {
        "name": '=HYPERLINK("http://evil")',
        "cost": None,
        "ends": dt.date(2026, 1, 2),
        "at": "",
        "n": "x",
    },
]


def test_csv_export_is_excel_friendly_and_formula_safe() -> None:
    r = export_response(ROWS, COLUMNS, filename="Device budget", format="csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "Device%20budget%20" in r.headers["content-disposition"]
    text = bytes(r.body).decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0] == ["Name", "Cost", "Ends", "At", "Count"]
    assert rows[1] == ["Laptop", "1250.50", "2027-01-31", "2026-10-14 13:05", "3"]
    assert rows[2][0].startswith("'=")  # never runs as a formula
    assert rows[2][4] == "x"  # not a number: kept as text, not dropped


def test_xlsx_export_keeps_types() -> None:
    r = export_response(ROWS, COLUMNS, filename="Device budget", format="xlsx")
    wb = load_workbook(io.BytesIO(bytes(r.body)))
    ws = wb.active
    assert ws is not None
    assert [c.value for c in ws[1]] == ["Name", "Cost", "Ends", "At", "Count"]
    assert ws.freeze_panes == "A2" and ws["A1"].font.bold
    assert ws["B2"].value == 1250.5 and "$" in ws["B2"].number_format
    assert ws["C2"].value == dt.datetime(2027, 1, 31)
    assert ws["D2"].value == dt.datetime(2026, 10, 14, 13, 5)
    assert ws["A3"].value == '=HYPERLINK("http://evil")' and ws["A3"].data_type == "s"


def test_unknown_export_format_is_refused() -> None:
    with pytest.raises(ExportProblem):
        export_response([], COLUMNS, filename="x", format="pdf")


# ---- field kinds -------------------------------------------------------------------------


def test_money_and_datetime_fields() -> None:
    t = TypeDef(
        name="Purchase",
        fields=[
            FieldDef(key="cost", label="Cost", kind="money"),
            FieldDef(key="at", label="At", kind="datetime"),
        ],
    )
    assert validate_values(t, {"cost": "$1,299.999", "at": "2026-10-14 13:05"}) == {
        "cost": 1300,
        "at": "2026-10-14T13:05",
    }
    assert validate_values(t, {"cost": "(12.50)"})["cost"] == -12.5
    with pytest.raises(TypeProblem, match="not an amount"):
        validate_values(t, {"cost": "twelve"})
    with pytest.raises(TypeProblem, match="date and time"):
        validate_values(t, {"at": "next tuesday"})


# ---- macros ------------------------------------------------------------------------------


def render(src: str, **ctx: object) -> str:
    from itops.core.config import Settings
    from itops.core.ui import build_templates

    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    env = build_templates([], settings).env
    return env.from_string('{% import "core/components.html" as ui %}' + src).render(**ctx)


def test_macros_render() -> None:
    html = render(
        "{{ ui.stat_tiles(tiles) }}"
        "{{ ui.chart('bar', ['A','B'], [{'name':'n','values':[1,2]}], 'Spend', money=True) }}"
        "{{ ui.breakdown(rows) }}{{ ui.export_links('/r?x=1') }}"
        "{{ ui.basis('estimate', 'no history yet') }}",
        tiles=[
            {
                "label": "Total",
                "value": "$1.00",
                "basis": "partial",
                "change": "+3%",
                "change_tone": "warning",
            }
        ],
        rows=[{"label": "Eng", "value": 3}, {"label": "QA", "value": 6, "display": "$6.00"}],
    )
    assert "Partly estimated" in html and "stat-change-warning" in html
    assert "<svg" in html and 'role="img"' in html and "Show numbers" in html and "$2.00" in html
    assert html.index("QA") < html.index("Eng")  # largest first
    assert "/r?x=1&amp;format=xlsx" in html and "format=csv" in html
    assert "Estimate" in html and "no history yet" in html
    assert "style=" not in html
