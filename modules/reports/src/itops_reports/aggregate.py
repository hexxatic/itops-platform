"""Bounded, permission-checked SQL summaries for large synced datasets."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import BigInteger, and_, cast, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from itops_reports import service
from itops_reports.app import module
from itops_reports.models import Row


def _utc_naive(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(dt.UTC).replace(tzinfo=None)


@module.service("aggregate_dataset")
async def aggregate_dataset(
    key: str,
    *,
    since: dt.datetime,
    windows: dict[str, dt.datetime],
    partition_field: str,
    filters: dict[str, str] | None = None,
    sum_fields: tuple[str, ...] = (),
    distinct_fields: tuple[str, ...] = (),
    group_field: str | None = None,
    group_sum_field: str | None = None,
    detail_limit: int = 200,
) -> dict[str, Any]:
    """Count and group all matching rows without the 200,000-row detail read cap.

    Each partition has its own exclusive completion boundary. This keeps unfinished
    connector pages out of a summary while allowing completed sources to appear.
    Field names must belong to the viewer's visible dataset columns.
    """
    module.require("reports.read")()
    if not 1 <= len(windows) <= 50 or not 0 <= detail_limit <= 200:
        raise service.ReportsError("Choose 1 to 50 sources and at most 200 detail rows.")
    if any(not name or len(name) > 200 for name in windows):
        raise service.ReportsError("Source names must be 1 to 200 characters.")
    exact = filters or {}
    async with module.session() as session:
        dataset = await service.dataset(session, key)
        if dataset is None:
            return {"available": False, "stats": {}, "groups": [], "rows": []}
        cols = await service.columns(dataset)
        visible = {field.key: field for field in cols.visible}
        requested = {
            partition_field,
            *exact.keys(),
            *sum_fields,
            *distinct_fields,
        }
        if group_field:
            requested.add(group_field)
        if group_sum_field:
            requested.add(group_sum_field)
        if any(field not in visible for field in requested):
            raise service.ReportsError("A requested dataset column is unavailable.")
        if any(visible[field].kind not in {"number", "money"} for field in sum_fields):
            raise service.ReportsError("Only number or visible money columns can be summed.")
        if group_field and (
            group_sum_field is None or visible[group_sum_field].kind not in {"number", "money"}
        ):
            raise service.ReportsError("A breakdown needs a number or visible money column.")

        def value(field: str) -> Any:
            # The path is a bound SQL argument, and the field was checked above.
            return func.json_value(Row.values, f"$.{field}")

        source = value(partition_field)
        windows_where = or_(
            *(
                and_(
                    func.lower(source) == name.casefold(),
                    Row.row_time < _utc_naive(through),
                )
                for name, through in windows.items()
            )
        )
        conditions = [
            Row.dataset_id == dataset.id,
            Row.row_time >= _utc_naive(since),
            windows_where,
            *(
                func.lower(value(field)) == expected.casefold()
                for field, expected in exact.items()
                if expected
            ),
        ]
        expressions: list[ColumnElement[Any]] = [func.count().label("rows")]
        labels = ["rows"]
        for field in sum_fields:
            expressions.append(func.coalesce(func.sum(cast(value(field), BigInteger)), 0))
            labels.append(field)
        for field in distinct_fields:
            expressions.append(func.count(func.distinct(func.nullif(func.lower(value(field)), ""))))
            labels.append(f"distinct_{field}")
        totals = (await session.execute(select(*expressions).where(*conditions))).one()
        stats = {label: int(totals[index] or 0) for index, label in enumerate(labels)}

        groups: list[dict[str, Any]] = []
        if group_field and group_sum_field:
            grouped_rows = (
                select(
                    value(group_field).label("group_name"),
                    cast(value(group_sum_field), BigInteger).label("measure"),
                )
                .where(*conditions)
                .subquery()
            )
            group_value = grouped_rows.c.group_name
            group_total = func.coalesce(func.sum(grouped_rows.c.measure), 0)
            grouped = await session.execute(
                select(group_value, group_total)
                .group_by(group_value)
                .order_by(group_total.desc(), group_value)
            )
            groups = [
                {group_field: str(name or ""), group_sum_field: int(total or 0)}
                for name, total in grouped.all()
            ]

        details = await session.execute(
            select(Row.values)
            .where(*conditions)
            .order_by(Row.row_time.desc(), Row.id.desc())
            .limit(detail_limit)
        )
        return {
            "available": True,
            "stats": stats,
            "groups": groups,
            "rows": [
                {
                    field: value
                    for field, value in json.loads(raw).items()
                    if field not in cols.hidden
                }
                for raw in details.scalars().all()
            ],
        }
