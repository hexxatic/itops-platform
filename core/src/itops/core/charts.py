"""Chart geometry for ``ui.chart`` (ADR 0016): the numbers in, positions out.

Charts are drawn on the server as SVG by the component library: no JavaScript, so they
print, follow light and dark mode (series colors are CSS classes on theme tokens) and
survive HTMX swaps. This module does the arithmetic Jinja is bad at: a readable value
axis, bar and point positions, thinned category labels. The macro only draws what it gets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

ChartKind = Literal["bar", "stacked", "line"]
SERIES_CLASSES = 6  # chart-s1 .. chart-s6 in app.css, from --chart-1 .. --chart-6
CURRENCY = "$"  # one currency per deployment

WIDTH = 640.0
TOP, RIGHT, BOTTOM = 10.0, 8.0, 26.0
MAX_X_LABELS = 12


def number(value: Any, decimals: int | None = None) -> str:
    """1234.5 -> "1,234.5"; whole numbers without decimals unless asked."""
    if value is None or value == "":
        return ""
    n = float(value)
    if decimals is None:
        decimals = 0 if n.is_integer() else 2
    return f"{n:,.{decimals}f}"


def money(value: Any) -> str:
    """1234.5 -> "$1,234.50"; negatives as -$12.00."""
    if value is None or value == "":
        return ""
    n = float(value)
    return f"{'-' if n < 0 else ''}{CURRENCY}{abs(n):,.2f}"


def compact(value: float, *, is_money: bool = False) -> str:
    """Axis labels: 950, 1.2k, 3.4M (money with the currency sign)."""
    n = abs(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            text = f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
            break
    else:
        text = f"{n:.2f}".rstrip("0").rstrip(".") if n else "0"
    sign = "-" if value < 0 else ""
    return f"{sign}{CURRENCY if is_money else ''}{text}"


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Round axis steps (1, 2, 2.5, 5 x 10^n) covering lo..hi, always including zero."""
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / max(count, 1)
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    start = math.floor(lo / step) * step
    ticks: list[float] = []
    t = start
    while t < hi + step * 0.999:
        ticks.append(round(t, 10))
        t += step
    return ticks


@dataclass
class _Series:
    name: str
    values: list[float | None]


def _series(raw: dict[str, Any], length: int) -> _Series:
    given: list[Any] = list(raw.get("values") or [])[:length]
    values: list[float | None] = [None if v is None else float(v) for v in given]
    return _Series(str(raw.get("name", "")), values + [None] * (length - len(values)))


def chart_layout(
    kind: ChartKind,
    labels: list[str],
    series: list[dict[str, Any]],
    *,
    height: int = 220,
    is_money: bool = False,
    unit: str = "",
) -> dict[str, Any]:
    """Positions for an SVG chart. ``series``: [{"name", "values"}], values aligned with
    ``labels`` (None for a gap). Returns everything the macro draws, plus the numbers as a
    table (for screen readers and copying)."""
    labels = [str(lbl) for lbl in labels]
    rows = [_series(s, len(labels)) for s in series]

    def fmt(v: float) -> str:
        return money(v) if is_money else number(v) + (f" {unit}" if unit else "")

    if kind == "stacked":
        tops = [sum(max(r.values[i] or 0, 0) for r in rows) for i in range(len(labels))]
        bottoms = [sum(min(r.values[i] or 0, 0) for r in rows) for i in range(len(labels))]
        lo, hi = min(bottoms, default=0.0), max(tops, default=0.0)
    else:
        found = [v for r in rows for v in r.values if v is not None]
        lo, hi = min(found, default=0.0), max(found, default=0.0)
    ticks = nice_ticks(lo, hi)
    tick_texts = [compact(t, is_money=is_money) for t in ticks]
    left = 12.0 + 7.0 * max((len(t) for t in tick_texts), default=1)
    plot = {"x": left, "y": TOP, "w": WIDTH - left - RIGHT, "h": float(height) - TOP - BOTTOM}
    span = (ticks[-1] - ticks[0]) or 1.0

    def y_of(v: float) -> float:
        return round(plot["y"] + plot["h"] * (ticks[-1] - v) / span, 2)

    zero = y_of(0.0)
    n = max(len(labels), 1)
    band = plot["w"] / n
    bars: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    if kind in ("bar", "stacked"):
        inner = band * 0.72
        for i, label in enumerate(labels):
            x0 = plot["x"] + band * i + (band - inner) / 2
            if kind == "bar":
                w = inner / max(len(rows), 1)
                for si, r in enumerate(rows):
                    v = r.values[i]
                    if v is None:
                        continue
                    y1, y2 = sorted((y_of(v), zero))
                    bars.append(
                        {
                            "x": round(x0 + w * si, 2),
                            "y": y1,
                            "w": round(max(w - 1, 1), 2),
                            "h": round(max(y2 - y1, 0.5), 2),
                            "series": si % SERIES_CLASSES + 1,
                            "title": f"{r.name} · {label}: {fmt(v)}",
                        }
                    )
            else:
                up, down = 0.0, 0.0
                for si, r in enumerate(rows):
                    v = r.values[i]
                    if not v:
                        continue
                    base = up if v > 0 else down
                    y1, y2 = sorted((y_of(base + v), y_of(base)))
                    if v > 0:
                        up += v
                    else:
                        down += v
                    bars.append(
                        {
                            "x": round(x0, 2),
                            "y": y1,
                            "w": round(inner, 2),
                            "h": round(max(y2 - y1, 0.5), 2),
                            "series": si % SERIES_CLASSES + 1,
                            "title": f"{r.name} · {label}: {fmt(v)}",
                        }
                    )
    else:
        for si, r in enumerate(rows):
            points = [
                {
                    "x": round(plot["x"] + band * i + band / 2, 2),
                    "y": y_of(v),
                    "title": f"{r.name} · {labels[i]}: {fmt(v)}",
                }
                for i, v in enumerate(r.values)
                if v is not None
            ]
            # Gaps break the line: one "M" per run of consecutive values.
            d: list[str] = []
            prev = -2
            for i, v in enumerate(r.values):
                if v is None:
                    continue
                x = round(plot["x"] + band * i + band / 2, 2)
                d.append(f"{'L' if prev == i - 1 else 'M'}{x},{y_of(v)}")
                prev = i
            lines.append({"d": " ".join(d), "points": points, "series": si % SERIES_CLASSES + 1})

    every = math.ceil(len(labels) / MAX_X_LABELS) if labels else 1
    x_labels = [
        {"x": round(plot["x"] + band * i + band / 2, 2), "text": label}
        for i, label in enumerate(labels)
        if i % every == 0
    ]
    return {
        "width": WIDTH,
        "height": float(height),
        "plot": plot,
        "zero": zero,
        "y_ticks": [{"y": y_of(t), "text": txt} for t, txt in zip(ticks, tick_texts, strict=True)],
        "x_labels": x_labels,
        "bars": bars,
        "lines": lines,
        "legend": [{"name": r.name, "series": i % SERIES_CLASSES + 1} for i, r in enumerate(rows)],
        "table": {
            "labels": labels,
            "rows": [
                {"name": r.name, "cells": [fmt(v) if v is not None else "" for v in r.values]}
                for r in rows
            ],
        },
    }
