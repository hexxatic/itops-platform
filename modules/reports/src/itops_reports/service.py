"""Reports rules (ADR 0016): datasets, their rows, report definitions, the services other
modules and connectors call, and the retention job. Pages, MCP tools and connectors all go
through here.

- **Datasets** have a record type for columns (``reports.types``), a mode (``synced``: only
  connectors write, through ``reports.upsert``; ``manual``: people add rows), an optional
  time column (trends and retention) and an optional retention period.
- **Money columns** need ``reports.costs`` and never reach AI connections: they're left out
  of rows, measures and exports for anyone else.
- **Rows AI proposes** wait as a batch on the dataset's page until a person approves (adds
  them as their own) or rejects them. AI can propose a typed-in dataset too (a person with
  ``reports.publish`` creates it), with rows that wait for it.
- **Report definitions** are published by people with ``reports.publish``; an AI connection
  can only save a draft, which such a person publishes or discards.
- **Areas** (ADR 0012): a dataset or a report can be restricted to AD groups. A hidden
  dataset hides its reports too. Hidden ones answer as if they didn't exist.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal, TypeProblem
from itops.sdk.types import TypeDef, validate_values
from itops_reports.app import module
from itops_reports.engine import Item, ReportConfig, ReportProblem, parse_config
from itops_reports.models import Dataset, DatasetProposal, Definition, Proposal, Row, utcnow

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,59}$")
MODES = {"manual": "Typed in by people", "synced": "Written by a connector"}
MAX_PASTE_ROWS = 2000
MAX_UPSERT_ROWS = 500


class ReportsError(ValueError):
    """Something a person (or AI) asked for that can't be done; the message says why."""


# ---- who ---


def _principal() -> Principal:
    p = module.current_principal()
    if p is None:
        raise ReportsError("Sign in first.")
    return p


def _who() -> str:
    p = module.current_principal()
    return p.user if p else "system"


def _require(permission: str, *, human: bool = False) -> Principal:
    p = _principal()
    if human and p.via_ai:
        raise ReportsError("An AI connection can't do this: a person does it in the web UI.")
    if not p.has(permission):
        raise ReportsError(f"You need {permission} for this.")
    return p


def can_see_money(p: Principal | None = None) -> bool:
    p = p or module.current_principal()
    return bool(p and not p.via_ai and p.has("reports.costs"))


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "report"
    return s if s[0].isalpha() else f"r-{s}"[:60]


# ---- datasets ---


@dataclass(frozen=True)
class Columns:
    """A dataset's columns: its type's published definition, and what this viewer can't see."""

    definition: TypeDef
    hidden: frozenset[str]

    @property
    def visible(self) -> list[Any]:
        return [f for f in self.definition.fields if f.key not in self.hidden]


async def columns(ds: Dataset) -> Columns:
    view = await module.types.get(ds.type_key)
    if view is None or view.published is None:
        raise ReportsError(f"The dataset's column type '{ds.type_key}' isn't published.")
    definition = view.published.definition
    money = frozenset(f.key for f in definition.fields if f.kind == "money")
    return Columns(definition, frozenset() if can_see_money() else money)


async def _hidden_datasets() -> frozenset[str]:
    return await module.areas.hidden("dataset")


async def datasets(s: AsyncSession) -> list[Dataset]:
    hidden = await _hidden_datasets()
    rows = (await s.execute(select(Dataset).order_by(Dataset.title))).scalars().all()
    return [d for d in rows if d.key not in hidden]


async def dataset(s: AsyncSession, key: str) -> Dataset | None:
    """A dataset the viewer may see (None otherwise, as if it didn't exist)."""
    if key in await _hidden_datasets():
        return None
    return await _dataset(s, key)


async def _dataset(s: AsyncSession, key: str) -> Dataset | None:
    return (await s.execute(select(Dataset).where(Dataset.key == key))).scalar_one_or_none()


async def row_counts(s: AsyncSession) -> dict[int, int]:
    rows = await s.execute(select(Row.dataset_id, func.count()).group_by(Row.dataset_id))
    return {int(d): int(n) for d, n in rows.all()}


@dataclass
class DatasetInput:
    title: str
    type_key: str
    mode: str = "manual"
    description: str = ""
    time_field: str | None = None
    retention_days: int | str | None = None
    key: str | None = None
    source: str = ""


async def _clean_dataset(data: DatasetInput) -> tuple[str, str, str, str | None, int | None]:
    title = " ".join(data.title.split())[:200]
    if not title:
        raise ReportsError("Give the dataset a title.")
    if data.mode not in MODES:
        raise ReportsError("Choose whether people type rows in or a connector writes them.")
    view = await module.types.get(data.type_key)
    if view is None or view.published is None:
        raise ReportsError("Choose a published column type for the dataset.")
    time_field = (data.time_field or "").strip() or None
    if time_field:
        kinds = {f.key: f.kind for f in view.published.definition.fields}
        if kinds.get(time_field) not in ("date", "datetime"):
            raise ReportsError("The time column must be a date or date-and-time column.")
    retention: int | None = None
    if data.retention_days not in (None, ""):
        try:
            retention = int(str(data.retention_days))
        except ValueError as exc:
            raise ReportsError(
                "Retention is a number of days (or empty to keep everything)."
            ) from exc
        if not 1 <= retention <= 36500:
            raise ReportsError("Retention is between 1 and 36500 days.")
        if not time_field:
            raise ReportsError("Retention needs a time column (rows are kept by their time).")
    return title, data.mode, " ".join(data.description.split())[:1000], time_field, retention


async def create_dataset(s: AsyncSession, data: DatasetInput) -> Dataset:
    _require("reports.publish", human=True)
    return await _create_dataset(s, data)


async def _create_dataset(s: AsyncSession, data: DatasetInput) -> Dataset:
    title, mode, description, time_field, retention = await _clean_dataset(data)
    key = data.key or slug(title)
    if not KEY_RE.match(key):
        raise ReportsError("A dataset key is lowercase letters, digits and dashes.")
    if await _dataset(s, key) is not None:
        raise ReportsError(f"There's already a dataset called '{key}'.")
    ds = Dataset(
        key=key,
        title=title,
        description=description,
        type_key=data.type_key,
        mode=mode,
        time_field=time_field,
        retention_days=retention,
        source=data.source[:200],
        created_by=_who(),
        updated_by=_who(),
    )
    s.add(ds)
    await s.flush()
    await module.audit.record(
        s,
        "dataset.created",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Created dataset “{ds.title}” ({MODES[mode].lower()})",
    )
    return ds


async def update_dataset(s: AsyncSession, ds: Dataset, data: DatasetInput) -> Dataset:
    _require("reports.publish", human=True)
    title, mode, description, time_field, retention = await _clean_dataset(data)
    if data.type_key != ds.type_key and await _count(s, ds) > 0:
        raise ReportsError("The column type can't change once the dataset has rows.")
    before = {
        "title": ds.title,
        "mode": ds.mode,
        "time_field": ds.time_field,
        "retention_days": ds.retention_days,
    }
    ds.title, ds.mode, ds.description, ds.type_key = title, mode, description, data.type_key
    reindex = ds.time_field != time_field
    ds.time_field, ds.retention_days = time_field, retention
    ds.updated_by, ds.updated_at = _who(), utcnow()
    if reindex:
        await _retime(s, ds)
    after = {
        "title": ds.title,
        "mode": ds.mode,
        "time_field": ds.time_field,
        "retention_days": ds.retention_days,
    }
    changed = [k for k in before if before[k] != after[k]]
    await module.audit.record(
        s,
        "dataset.updated",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary="Changed " + (", ".join(changed) or "the description"),
        details={"before": before, "after": after},
    )
    return ds


async def _count(s: AsyncSession, ds: Dataset) -> int:
    return int((await s.execute(select(func.count()).where(Row.dataset_id == ds.id))).scalar_one())


def row_time(ds: Dataset, values: dict[str, Any]) -> dt.datetime | None:
    raw = values.get(ds.time_field) if ds.time_field else None
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw)).replace(tzinfo=None)
    except ValueError:
        return None


async def _retime(s: AsyncSession, ds: Dataset) -> None:
    for r in (await s.execute(select(Row).where(Row.dataset_id == ds.id))).scalars():
        r.row_time = row_time(ds, json.loads(r.values))


# ---- rows ---


def _strip(values: dict[str, Any], cols: Columns) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k not in cols.hidden}


async def rows_page(
    s: AsyncSession, ds: Dataset, *, limit: int = 200
) -> list[tuple[Row, dict[str, Any]]]:
    cols = await columns(ds)
    q = (
        select(Row)
        .where(Row.dataset_id == ds.id)
        .order_by(Row.row_time.desc(), Row.id.desc())
        .limit(limit)
    )
    return [(r, _strip(json.loads(r.values), cols)) for r in (await s.execute(q)).scalars()]


async def count(s: AsyncSession, ds: Dataset) -> int:
    return await _count(s, ds)


async def items(
    s: AsyncSession, ds: Dataset, *, since: dt.datetime | None = None
) -> tuple[list[Item], bool]:
    """The dataset's rows for the engine (money stripped for viewers without reports.costs),
    newest first, capped at engine.MAX_ROWS. Returns (items, truncated)."""
    from itops_reports.engine import MAX_ROWS

    cols = await columns(ds)
    q = select(Row.id, Row.row_time, Row.values).where(Row.dataset_id == ds.id)
    if since is not None and ds.time_field:
        q = q.where(Row.row_time >= since)
    q = q.order_by(Row.row_time.desc(), Row.id.desc()).limit(MAX_ROWS + 1)
    found = (await s.execute(q)).all()
    out = [Item(int(i), _strip(json.loads(v), cols), t) for i, t, v in found[:MAX_ROWS]]
    return out, len(found) > MAX_ROWS


async def get_row(s: AsyncSession, ds: Dataset, row_id: int) -> Row | None:
    r = await s.get(Row, row_id)
    return r if r is not None and r.dataset_id == ds.id else None


def _manual(ds: Dataset) -> None:
    if ds.mode != "manual":
        raise ReportsError("A connector writes this dataset: its rows can't be changed here.")


async def _validated(
    ds: Dataset, values: dict[str, Any], keep: dict[str, Any] | None = None
) -> dict[str, Any]:
    cols = await columns(ds)
    given = {k: v for k, v in values.items() if k not in cols.hidden}
    try:
        clean = await module.types.validate(ds.type_key, given)
    except TypeProblem as exc:
        raise ReportsError(str(exc)) from exc
    if keep:  # money a viewer couldn't see stays as it was
        clean.update({k: v for k, v in keep.items() if k in cols.hidden})
    return clean


async def add_row(s: AsyncSession, ds: Dataset, values: dict[str, Any]) -> Row:
    p = _require("reports.write")
    _manual(ds)
    if p.via_ai:
        raise ReportsError(
            "AI connections propose rows instead (reports_propose_rows): a person approves them."
        )
    clean = await _validated(ds, values)
    r = Row(
        dataset_id=ds.id,
        values=json.dumps(clean),
        row_time=row_time(ds, clean),
        created_by=_who(),
        updated_by=_who(),
    )
    s.add(r)
    await s.flush()
    await module.audit.record(
        s,
        "row.added",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Added a row (#{r.id})",
        details={"row": r.id},
    )
    module.publish("rows_changed", {"dataset": ds.key, "added": 1, "changed": 0, "deleted": 0})
    return r


async def update_row(s: AsyncSession, ds: Dataset, r: Row, values: dict[str, Any]) -> Row:
    p = _require("reports.write")
    _manual(ds)
    if p.via_ai:
        raise ReportsError("AI connections can't change rows: a person does.")
    before = json.loads(r.values)
    clean = await _validated(ds, values, keep=before)
    changed = sorted(k for k in set(before) | set(clean) if before.get(k) != clean.get(k))
    r.values, r.row_time = json.dumps(clean), row_time(ds, clean)
    r.updated_by, r.updated_at = _who(), utcnow()
    hidden = (await columns(ds)).hidden
    await module.audit.record(
        s,
        "row.changed",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Changed row #{r.id}: {', '.join(changed) or 'nothing'}",
        details={
            "row": r.id,
            "before": {k: v for k, v in before.items() if k not in hidden},
            "after": {k: v for k, v in clean.items() if k not in hidden},
        },
    )
    module.publish("rows_changed", {"dataset": ds.key, "added": 0, "changed": 1, "deleted": 0})
    return r


async def delete_row(s: AsyncSession, ds: Dataset, r: Row) -> None:
    p = _require("reports.write")
    _manual(ds)
    if p.via_ai:
        raise ReportsError("AI connections can't delete rows: a person does.")
    await s.delete(r)
    await module.audit.record(
        s,
        "row.deleted",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Deleted row #{r.id}",
        details={"row": r.id},
    )
    module.publish("rows_changed", {"dataset": ds.key, "added": 0, "changed": 0, "deleted": 1})


@dataclass
class Pasted:
    """Rows read from pasted spreadsheet text, before saving."""

    rows: list[dict[str, Any]]
    problems: list[str]
    columns: list[str]  # field keys, in the order the header named them


async def read_paste(ds: Dataset, text: str) -> Pasted:
    """Parse rows copied from a spreadsheet (tab-separated, or CSV). The first line names the
    columns, by label or key, in any order; unknown headers are a problem, missing columns
    stay empty. Every row is validated; nothing is saved here."""
    _manual(ds)
    cols = await columns(ds)
    text = text.strip("\r\n")
    if not text.strip():
        raise ReportsError("Paste the rows first, with the column names on the first line.")
    dialect = "excel-tab" if "\t" in text.splitlines()[0] else "excel"
    lines = list(csv.reader(io.StringIO(text), dialect=dialect))
    by_name = {f.label.lower(): f.key for f in cols.visible} | {
        f.key.lower(): f.key for f in cols.visible
    }
    header = [h.strip() for h in lines[0]]
    keys: list[str | None] = [by_name.get(h.lower()) for h in header]
    problems = [
        f"Unknown column “{h}” (columns: {', '.join(f.label for f in cols.visible)})"
        for h, k in zip(header, keys, strict=True)
        if h and k is None
    ]
    data = [ln for ln in lines[1:] if any(c.strip() for c in ln)]
    if len(data) > MAX_PASTE_ROWS:
        raise ReportsError(f"At most {MAX_PASTE_ROWS} rows at once.")
    out: list[dict[str, Any]] = []
    for n, ln in enumerate(data, start=2):
        raw = {k: v.strip() for k, v in zip(keys, ln, strict=False) if k and v.strip()}
        try:
            out.append(await module.types.validate(ds.type_key, raw))
        except TypeProblem as exc:
            problems.append(f"Line {n}: {exc}")
    return Pasted(out, problems, [k for k in keys if k])


async def save_paste(s: AsyncSession, ds: Dataset, text: str) -> int:
    p = _require("reports.write")
    if p.via_ai:
        raise ReportsError(
            "AI connections propose rows instead (reports_propose_rows): a person approves them."
        )
    pasted = await read_paste(ds, text)
    if pasted.problems:
        raise ReportsError("Fix these first: " + "; ".join(pasted.problems[:10]))
    await _insert(s, ds, pasted.rows)
    await module.audit.record(
        s,
        "row.pasted",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Added {len(pasted.rows)} rows (pasted from a spreadsheet)",
    )
    return len(pasted.rows)


async def _insert(s: AsyncSession, ds: Dataset, rows: list[dict[str, Any]]) -> None:
    for values in rows:
        s.add(
            Row(
                dataset_id=ds.id,
                values=json.dumps(values),
                row_time=row_time(ds, values),
                created_by=_who(),
                updated_by=_who(),
            )
        )
    await s.flush()
    module.publish(
        "rows_changed", {"dataset": ds.key, "added": len(rows), "changed": 0, "deleted": 0}
    )


# ---- rows AI proposes ---


async def propose_rows(
    s: AsyncSession,
    ds: Dataset,
    rows: list[dict[str, Any]],
    note: str,
    *,
    waiting_on: DatasetProposal | None = None,
) -> Proposal:
    """AI (or anyone with reports.write) proposes rows for a typed-in dataset; nothing is
    added until a person approves them. Columns are named by key or label. Money columns may
    be given: proposing a value isn't seeing one, and AI never reads them back. For a dataset
    that is only proposed (``waiting_on``), ``ds`` is ``proposed_dataset(...)`` and the rows
    wait until it's created."""
    _require("reports.write")
    _manual(ds)
    note = " ".join(note.split())[:1000]
    if not note:
        raise ReportsError("Say where the rows come from (note), so the reviewer can check them.")
    if not rows:
        raise ReportsError("Propose at least one row.")
    if len(rows) > MAX_PASTE_ROWS:
        raise ReportsError(f"At most {MAX_PASTE_ROWS} rows at once.")
    definition = (await columns(ds)).definition
    by_name = {f.label.lower(): f.key for f in definition.fields} | {
        f.key.lower(): f.key for f in definition.fields
    }
    clean: list[dict[str, Any]] = []
    problems: list[str] = []
    for n, given in enumerate(rows, start=1):
        raw: dict[str, Any] = {}
        for name, value in given.items():
            key = by_name.get(str(name).strip().lower())
            if key is None:
                problems.append(f"Row {n}: unknown column “{name}”")
            elif value not in (None, ""):
                raw[key] = value.strip() if isinstance(value, str) else value
        try:
            clean.append(await module.types.validate(ds.type_key, raw))
        except TypeProblem as exc:
            problems.append(f"Row {n}: {exc}")
    if problems:
        more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
        raise ReportsError("Fix these first: " + "; ".join(problems[:10]) + more)
    p = Proposal(
        dataset_id=None if waiting_on else ds.id,
        dataset_proposal_id=waiting_on.id if waiting_on else None,
        rows=json.dumps(clean),
        row_count=len(clean),
        note=note,
        proposed_by=_who(),
    )
    s.add(p)
    await s.flush()
    await module.audit.record(
        s,
        "rows.proposed",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Proposed {len(clean)} rows (waiting for a person to approve them"
        + (", once the dataset is created)" if waiting_on else ")"),
        details={"proposal": p.id},
    )
    return p


def proposed_rows(p: Proposal) -> list[dict[str, Any]]:
    return list(json.loads(p.rows))


async def proposals(s: AsyncSession, ds: Dataset | None = None) -> list[tuple[Proposal, Dataset]]:
    """Pending proposals in datasets the viewer may see, oldest first."""
    hidden = await _hidden_datasets()
    q = (
        select(Proposal, Dataset)
        .join(Dataset, Proposal.dataset_id == Dataset.id)
        .where(Proposal.status == "pending")
        .order_by(Proposal.proposed_at, Proposal.id)
    )
    if ds is not None:
        q = q.where(Proposal.dataset_id == ds.id)
    return [(p, d) for p, d in (await s.execute(q)).tuples() if d.key not in hidden]


async def get_proposal(s: AsyncSession, proposal_id: int) -> tuple[Proposal, Dataset] | None:
    p = await s.get(Proposal, proposal_id)
    if p is None or p.dataset_id is None:
        return None
    ds = await s.get(Dataset, p.dataset_id)
    if ds is None or ds.key in await _hidden_datasets():
        return None
    return p, ds


async def already_in(s: AsyncSession, ds: Dataset, rows: list[dict[str, Any]]) -> int:
    """How many of these rows match a row the dataset already has (to catch a batch proposed
    twice)."""

    def canon(values: dict[str, Any]) -> str:
        return json.dumps(values, sort_keys=True, default=str)

    have = {
        canon(json.loads(v))
        for v in (await s.execute(select(Row.values).where(Row.dataset_id == ds.id))).scalars()
    }
    return sum(canon(r) in have for r in rows)


async def has_money(ds: Dataset, p: Proposal) -> bool:
    definition = (await columns(ds)).definition
    money = {f.key for f in definition.fields if f.kind == "money"}
    return any(r.get(k) not in (None, "") for r in proposed_rows(p) for k in money)


def _decide(p: Proposal | DatasetProposal, status: str) -> None:
    if p.status != "pending":
        raise ReportsError(f"That proposal was already {p.status}.")
    p.status, p.decided_by, p.decided_at = status, _who(), utcnow()


async def approve_proposal(s: AsyncSession, p: Proposal, ds: Dataset) -> int:
    """A person adds the proposed rows, as their own (checked again against the columns)."""
    _require("reports.publish", human=True)
    _manual(ds)
    if await has_money(ds, p) and not can_see_money():
        raise ReportsError("These rows include money: someone who can see costs approves them.")
    _decide(p, "approved")
    rows: list[dict[str, Any]] = []
    for n, values in enumerate(proposed_rows(p), start=1):
        try:
            rows.append(await module.types.validate(ds.type_key, values))
        except TypeProblem as exc:
            raise ReportsError(
                f"Row {n} no longer fits the dataset's columns: {exc}. Reject it instead."
            ) from exc
    await _insert(s, ds, rows)
    await module.audit.record(
        s,
        "rows.approved",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Added {len(rows)} rows proposed by {p.proposed_by}",
        details={"proposal": p.id},
    )
    return len(rows)


async def reject_proposal(s: AsyncSession, p: Proposal, ds: Dataset) -> None:
    _require("reports.publish", human=True)
    _decide(p, "rejected")
    await module.audit.record(
        s,
        "rows.rejected",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Rejected {p.row_count} rows proposed by {p.proposed_by}",
        details={"proposal": p.id},
    )


# ---- datasets AI proposes ---


def dataset_spec(dp: DatasetProposal) -> DatasetInput:
    return DatasetInput(**json.loads(dp.spec))


def proposed_dataset(dp: DatasetProposal) -> Dataset:
    """A stand-in (never saved) for checking rows proposed for a dataset not yet created."""
    spec = dataset_spec(dp)
    return Dataset(
        key=dp.key,
        title=spec.title,
        type_key=spec.type_key,
        mode="manual",
        time_field=spec.time_field,
    )


async def propose_dataset(s: AsyncSession, data: DatasetInput, note: str) -> DatasetProposal:
    """AI (or anyone with reports.write) proposes a typed-in dataset; a person with
    reports.publish creates it. Checked now as if it were being created."""
    _require("reports.write")
    if data.mode != "manual":
        raise ReportsError("Only typed-in datasets can be proposed: connectors create their own.")
    note = " ".join(note.split())[:1000]
    if not note:
        raise ReportsError("Say what the dataset is for (note), so the reviewer can check it.")
    title, _, description, time_field, retention = await _clean_dataset(data)
    key = data.key or slug(title)
    if not KEY_RE.match(key):
        raise ReportsError("A dataset key is lowercase letters, digits and dashes.")
    if await _dataset(s, key) is not None:
        raise ReportsError(f"There's already a dataset called '{key}'.")
    if await pending_dataset(s, key) is not None:
        raise ReportsError(f"A dataset called '{key}' is already proposed.")
    spec = {
        "title": title,
        "key": key,
        "type_key": data.type_key,
        "description": description,
        "time_field": time_field,
        "retention_days": retention,
    }
    dp = DatasetProposal(key=key, spec=json.dumps(spec), note=note, proposed_by=_who())
    s.add(dp)
    await s.flush()
    await module.audit.record(
        s,
        "dataset.proposed",
        kind="dataset",
        entity_id=key,
        title=title,
        summary=f"Proposed dataset “{title}” (waiting for an admin to create it)",
        details={"dataset_proposal": dp.id},
    )
    return dp


async def pending_dataset(s: AsyncSession, key: str) -> DatasetProposal | None:
    q = select(DatasetProposal).where(
        DatasetProposal.key == key, DatasetProposal.status == "pending"
    )
    return (await s.execute(q)).scalars().first()


async def dataset_proposals(s: AsyncSession) -> list[DatasetProposal]:
    q = (
        select(DatasetProposal)
        .where(DatasetProposal.status == "pending")
        .order_by(DatasetProposal.proposed_at, DatasetProposal.id)
    )
    return list((await s.execute(q)).scalars())


async def get_dataset_proposal(s: AsyncSession, proposal_id: int) -> DatasetProposal | None:
    return await s.get(DatasetProposal, proposal_id)


async def rows_waiting(s: AsyncSession) -> list[tuple[Proposal, DatasetProposal]]:
    """Row batches waiting for a proposed dataset to be created."""
    q = (
        select(Proposal, DatasetProposal)
        .join(DatasetProposal, Proposal.dataset_proposal_id == DatasetProposal.id)
        .where(Proposal.status == "pending", Proposal.dataset_id.is_(None))
        .order_by(Proposal.proposed_at, Proposal.id)
    )
    return [(p, dp) for p, dp in (await s.execute(q)).tuples()]


async def approve_dataset(s: AsyncSession, dp: DatasetProposal) -> Dataset:
    """Creates the dataset as the person approving; rows proposed for it move to its page,
    still waiting for their own approval."""
    _require("reports.publish", human=True)
    _decide(dp, "approved")
    ds = await _create_dataset(s, dataset_spec(dp))
    for p, _ in await rows_waiting(s):
        if p.dataset_proposal_id == dp.id:
            p.dataset_id = ds.id
    await module.audit.record(
        s,
        "dataset.proposal_approved",
        kind="dataset",
        entity_id=ds.key,
        title=ds.title,
        summary=f"Created the dataset proposed by {dp.proposed_by}",
        details={"dataset_proposal": dp.id},
    )
    return ds


async def reject_dataset(s: AsyncSession, dp: DatasetProposal) -> None:
    """Rejects the dataset and any rows waiting for it."""
    _require("reports.publish", human=True)
    _decide(dp, "rejected")
    for p, _ in await rows_waiting(s):
        if p.dataset_proposal_id == dp.id:
            _decide(p, "rejected")
    await module.audit.record(
        s,
        "dataset.proposal_rejected",
        kind="dataset",
        entity_id=dp.key,
        title=dataset_spec(dp).title,
        summary=f"Rejected the dataset proposed by {dp.proposed_by}",
        details={"dataset_proposal": dp.id},
    )


# ---- report definitions ---


async def _hidden_reports() -> frozenset[str]:
    return await module.areas.hidden("report")


async def definitions(s: AsyncSession, *, drafts: bool = False) -> list[tuple[Definition, Dataset]]:
    """Reports the viewer may see: published ones (and drafts for people who publish)."""
    hidden_r, hidden_d = await _hidden_reports(), await _hidden_datasets()
    q = (
        select(Definition, Dataset)
        .join(Dataset, Definition.dataset_id == Dataset.id)
        .order_by(Definition.title)
    )
    out: list[tuple[Definition, Dataset]] = []
    for d, ds in (await s.execute(q)).tuples():
        if d.key in hidden_r or ds.key in hidden_d:
            continue
        if d.config is None and not drafts:
            continue
        out.append((d, ds))
    return out


async def definition(s: AsyncSession, key: str) -> tuple[Definition, Dataset] | None:
    if key in await _hidden_reports():
        return None
    found = (
        (
            await s.execute(
                select(Definition, Dataset)
                .join(Dataset, Definition.dataset_id == Dataset.id)
                .where(Definition.key == key)
            )
        )
        .tuples()
        .first()
    )
    if found is None or found[1].key in await _hidden_datasets():
        return None
    return found


async def config_of(d: Definition, ds: Dataset, *, draft: bool = False) -> ReportConfig:
    raw = d.draft_config if draft and d.draft_config else d.config
    cols = await columns(ds)
    return parse_config(json.loads(raw or "{}"), cols.definition, time_field=ds.time_field)


@dataclass
class DefinitionInput:
    title: str
    dataset: str
    config: dict[str, Any]
    description: str = ""
    key: str | None = None


async def save_definition(
    s: AsyncSession, data: DefinitionInput, existing: Definition | None = None
) -> Definition:
    """People with reports.publish save (and so publish) directly; an AI connection with
    reports.write saves a draft, which such a person publishes or discards."""
    p = _principal()
    via_ai = p.via_ai
    if via_ai:
        _require("reports.write")
    else:
        _require("reports.publish", human=True)
    ds = await dataset(s, data.dataset)
    if ds is None:
        raise ReportsError(f"No dataset '{data.dataset}'.")
    if existing is not None and existing.dataset_id != ds.id:
        raise ReportsError("A report's dataset can't change: make a new report instead.")
    title = " ".join(data.title.split())[:200]
    if not title:
        raise ReportsError("Give the report a title.")
    cols = await columns(ds)
    try:
        cfg = parse_config(data.config, cols.definition, time_field=ds.time_field)
    except ReportProblem as exc:
        raise ReportsError(str(exc)) from exc
    body = cfg.model_dump_json(exclude_defaults=True)
    description = " ".join(data.description.split())[:1000]
    if existing is None:
        key = data.key or slug(title)
        if not KEY_RE.match(key):
            raise ReportsError("A report key is lowercase letters, digits and dashes.")
        if (await s.execute(select(Definition.id).where(Definition.key == key))).first():
            raise ReportsError(f"There's already a report called '{key}'.")
        d = Definition(
            key=key,
            title=title,
            description=description,
            dataset_id=ds.id,
            created_by=_who(),
            updated_by=_who(),
        )
        s.add(d)
    else:
        d = existing
        if not via_ai:
            d.title, d.description = title, description
    d.updated_by, d.updated_at = _who(), utcnow()
    if via_ai:
        d.draft_config, d.draft_by, d.draft_via_ai = body, _who(), True
        if existing is None:
            d.title, d.description = title, description
        action, summary = "report.drafted", f"Drafted report “{title}” (waiting to be published)"
    else:
        d.config, d.draft_config, d.draft_by, d.draft_via_ai = body, None, None, False
        d.published_by, d.published_at = _who(), utcnow()
        action, summary = "report.published", f"Published report “{title}”"
    await s.flush()
    await module.audit.record(
        s, action, kind="report", entity_id=d.key, title=title, summary=summary
    )
    return d


def stamp(d: Definition) -> str:
    """When the report last changed. The draft view sends it back with Publish or Discard
    draft, so a draft changed after the page loaded (an AI connection may) isn't acted on
    unread."""
    return d.updated_at.isoformat()


def _check_reviewed(d: Definition, reviewed: str | None, done: str, verb: str) -> None:
    """``reviewed`` is the stamp the person's page showed; ``None`` only for callers
    without a page (tests)."""
    if reviewed is not None and reviewed != stamp(d):
        raise ReportsError(
            f"Not {done}: {d.draft_by or d.updated_by} changed this report at "
            f"{d.updated_at:%Y-%m-%d %H:%M} UTC, after you opened it. The draft is shown "
            f"below: check it, then {verb} again."
        )


async def publish_draft(
    s: AsyncSession, d: Definition, ds: Dataset, *, reviewed: str | None
) -> None:
    """Publish the drafted changes: ``reviewed`` is :func:`stamp` as the draft view showed it."""
    _require("reports.publish", human=True)
    if not d.draft_config:
        raise ReportsError("There's no draft to publish.")
    _check_reviewed(d, reviewed, "published", "publish")
    await config_of(d, ds, draft=True)  # still fits the dataset
    d.config, d.draft_config, d.draft_by, d.draft_via_ai = d.draft_config, None, None, False
    d.published_by, d.published_at, d.updated_by, d.updated_at = _who(), utcnow(), _who(), utcnow()
    await module.audit.record(
        s,
        "report.published",
        kind="report",
        entity_id=d.key,
        title=d.title,
        summary=f"Published the draft of “{d.title}”",
    )


async def discard_draft(s: AsyncSession, d: Definition, *, reviewed: str | None) -> bool:
    """Drops the draft; a report that was never published goes with it. True if deleted."""
    _require("reports.publish", human=True)
    _check_reviewed(d, reviewed, "discarded", "discard it")
    if d.config is None:
        await s.delete(d)
        await module.audit.record(
            s,
            "report.discarded",
            kind="report",
            entity_id=d.key,
            title=d.title,
            summary=f"Discarded the drafted report “{d.title}”",
        )
        return True
    d.draft_config, d.draft_by, d.draft_via_ai = None, None, False
    await module.audit.record(
        s,
        "report.draft_discarded",
        kind="report",
        entity_id=d.key,
        title=d.title,
        summary=f"Discarded draft changes to “{d.title}”",
    )
    return False


async def delete_definition(s: AsyncSession, d: Definition) -> None:
    _require("reports.publish", human=True)
    await s.delete(d)
    await module.audit.record(
        s,
        "report.deleted",
        kind="report",
        entity_id=d.key,
        title=d.title,
        summary=f"Deleted report “{d.title}”",
    )


# ---- services (other modules and connectors) ---


@module.service("ensure_dataset")
async def ensure_dataset(
    key: str,
    title: str,
    type_key: str,
    *,
    time_field: str | None = None,
    retention_days: int | None = None,
    description: str = "",
) -> dict[str, Any]:
    """A connector makes sure its (synced) dataset exists; an existing one is left as admins
    set it. Writes nothing in a dry run. Returns {key, created}."""
    run = module.refs.run
    async with module.session() as s:
        if await _dataset(s, key) is not None:
            return {"key": key, "created": False}
        if run is None:
            raise ReportsError("Only a connector run can create its dataset this way.")
        if run.dry_run:
            return {"key": key, "created": False, "dry_run": True}
        await _create_dataset(
            s,
            DatasetInput(
                title=title,
                type_key=type_key,
                mode="synced",
                key=key,
                description=description,
                time_field=time_field,
                retention_days=retention_days,
                source=run.connector,
            ),
        )
        await s.commit()
    return {"key": key, "created": True}


@module.service("upsert")
async def upsert(dataset: str, source_key: str, values: dict[str, Any]) -> dict[str, Any]:
    """Connectors only (ADR 0010): add or update one row of a synced dataset, matched by the
    source's key. Unchanged rows aren't rewritten; a dry run writes nothing. Returns
    {outcome: created|updated|unchanged|invalid, problem?} for ``ctx.apply``."""
    run = module.refs.run
    if run is None:
        raise ReportsError("Only a connector run writes synced rows.")
    if "*" not in (module.refs.owns("rows") or frozenset()):
        raise ReportsError(f'{run.connector} must declare owns = {{ "reports.rows" = ["*"] }}.')
    source_key = str(source_key)[:200]
    async with module.session() as s:
        ds = await _dataset(s, dataset)
        if ds is None:
            return {
                "outcome": "invalid",
                "problem": f"No dataset '{dataset}' (call reports.ensure_dataset first).",
            }
        if ds.mode != "synced":
            return {
                "outcome": "invalid",
                "problem": f"'{dataset}' is typed in by people, not synced.",
            }
        try:
            clean = await module.types.validate(ds.type_key, values)
        except TypeProblem as exc:
            return {"outcome": "invalid", "problem": f"{source_key}: {exc}"}
        print_ = module.refs.fingerprint(clean)
        existing = (
            await s.execute(
                select(Row).where(Row.dataset_id == ds.id, Row.source_key == source_key)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.fingerprint == print_:
            return {"outcome": "unchanged"}
        outcome = "created" if existing is None else "updated"
        if run.dry_run:
            return {"outcome": outcome}
        if existing is None:
            s.add(
                Row(
                    dataset_id=ds.id,
                    source_key=source_key,
                    fingerprint=print_,
                    values=json.dumps(clean),
                    row_time=row_time(ds, clean),
                    created_by=run.connector,
                    updated_by=run.connector,
                )
            )
        else:
            existing.values, existing.fingerprint = json.dumps(clean), print_
            existing.row_time, existing.updated_by, existing.updated_at = (
                row_time(ds, clean),
                run.connector,
                utcnow(),
            )
        await s.commit()
    return {"outcome": outcome}


@module.service("upsert_many")
async def upsert_many(dataset: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Connectors only: validate and upsert one bounded page of synced rows in one
    transaction. Each item is {source_key, values}. Returns per-outcome counts, or one
    invalid result; a rejected page writes nothing."""
    run = module.refs.run
    if run is None:
        raise ReportsError("Only a connector run writes synced rows.")
    if "*" not in (module.refs.owns("rows") or frozenset()):
        raise ReportsError(f'{run.connector} must declare owns = {{ "reports.rows" = ["*"] }}.')
    if len(rows) > MAX_UPSERT_ROWS:
        return {
            "outcome": "invalid",
            "problem": f"A connector page may contain at most {MAX_UPSERT_ROWS} rows.",
        }
    if not rows:
        return {"counts": {}}

    source_rows: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for item in rows:
        source_key = str(item.get("source_key", ""))[:200]
        if not source_key or source_key in seen:
            return {"outcome": "invalid", "problem": "A page has a missing or repeated source key."}
        seen.add(source_key)
        values = item.get("values")
        if not isinstance(values, dict):
            return {"outcome": "invalid", "problem": f"{source_key}: row values must be an object."}
        source_rows.append((source_key, cast(dict[str, Any], values)))

    async with module.session() as s:
        ds = await _dataset(s, dataset)
        if ds is None:
            return {
                "outcome": "invalid",
                "problem": f"No dataset '{dataset}' (call reports.ensure_dataset first).",
            }
        if ds.mode != "synced":
            return {
                "outcome": "invalid",
                "problem": f"'{dataset}' is typed in by people, not synced.",
            }
        type_view = await module.types.get(ds.type_key)
        if type_view is None or type_view.published is None:
            return {"outcome": "invalid", "problem": "The dataset's column type is not published."}
        definition = type_view.published.definition

        cleaned: list[tuple[str, dict[str, Any], str]] = []
        for source_key, values in source_rows:
            try:
                value = validate_values(definition, values)
            except TypeProblem as exc:
                return {"outcome": "invalid", "problem": f"{source_key}: {exc}"}
            cleaned.append((source_key, value, module.refs.fingerprint(value)))

        keys = [source_key for source_key, _, _ in cleaned]
        existing = {
            row.source_key: row
            for row in (
                await s.execute(
                    select(Row).where(Row.dataset_id == ds.id, Row.source_key.in_(keys))
                )
            ).scalars()
        }
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        changed = False
        for source_key, value, fingerprint in cleaned:
            row = existing.get(source_key)
            if row is not None and row.fingerprint == fingerprint:
                counts["unchanged"] += 1
                continue
            outcome = "updated" if row is not None else "created"
            counts[outcome] += 1
            if run.dry_run:
                continue
            serialized = json.dumps(value)
            if row is None:
                s.add(
                    Row(
                        dataset_id=ds.id,
                        source_key=source_key,
                        fingerprint=fingerprint,
                        values=serialized,
                        row_time=row_time(ds, value),
                        created_by=run.connector,
                        updated_by=run.connector,
                    )
                )
            else:
                row.values, row.fingerprint = serialized, fingerprint
                row.row_time, row.updated_by, row.updated_at = (
                    row_time(ds, value),
                    run.connector,
                    utcnow(),
                )
            changed = True
        if changed:
            await s.commit()
    return {"counts": counts}


@module.service("rows")
async def rows(dataset: str, *, since: dt.datetime | None = None) -> list[dict[str, Any]]:
    """A dataset's rows for another module (e.g. budget): values plus ``_time``. Respects
    the current viewer: [] for a dataset they can't see; money only with reports.costs."""
    async with module.session() as s:
        ds = await dataset_or_none(s, dataset)
        if ds is None:
            return []
        found, _ = await items(s, ds, since=since)
    return [{**it.values, "_time": it.time} for it in found]


@module.service("read_dataset")
async def read_dataset(key: str, *, since: dt.datetime | None = None) -> dict[str, Any]:
    """Permission-checked rows with explicit availability and truncation for dashboards.

    Missing and hidden datasets have the same response. Unlike rows(), this preserves
    the engine's truncation signal so callers never mistake a partial total for a full one.
    """
    _require("reports.read")
    async with module.session() as s:
        ds = await dataset_or_none(s, key)
        if ds is None:
            return {"available": False, "rows": [], "truncated": False}
        found, truncated = await items(s, ds, since=since)
    return {
        "available": True,
        "truncated": truncated,
        "rows": [{**it.values, "_time": it.time} for it in found],
    }


async def dataset_or_none(s: AsyncSession, key: str) -> Dataset | None:
    return await dataset(s, key)


@module.service("describe_area")
async def describe_area(kind: str, key: str) -> dict[str, str] | None:
    async with module.session() as s:
        if kind == "dataset":
            ds = await _dataset(s, key)
            return {"name": ds.title, "url": module.url(f"/d/{ds.key}")} if ds else None
        if kind == "report":
            d = (
                await s.execute(select(Definition).where(Definition.key == key))
            ).scalar_one_or_none()
            return {"name": d.title, "url": module.url(f"/r/{d.key}")} if d else None
    return None


@module.service("area_entities")
async def area_entities(kind: str, key: str) -> dict[str, Any]:
    """Reports put nothing in search, so no search entries belong to an area."""
    return {"kind": kind, "ids": []}


# ---- retention ---


@module.job("retention")
async def retention() -> str | None:
    removed: list[str] = []
    async with module.session() as s:
        for ds in (
            await s.execute(select(Dataset).where(Dataset.retention_days.is_not(None)))
        ).scalars():
            assert ds.retention_days is not None
            cutoff = utcnow() - dt.timedelta(days=ds.retention_days)
            result = await s.execute(
                delete(Row).where(Row.dataset_id == ds.id, Row.row_time < cutoff)
            )
            n = int(getattr(result, "rowcount", 0) or 0)
            if n:
                removed.append(f"{ds.title}: {n}")
            await s.commit()
    return ("Removed old rows: " + "; ".join(removed)) if removed else None


@module.service("waiting")
async def waiting() -> list[dict[str, Any]]:
    """What's waiting for the person looking (core's *Waiting for you*): AI's report drafts
    and proposed datasets to publish, and proposed rows to add."""
    who = module.current_principal()
    out: list[dict[str, Any]] = []
    if who is None:
        return out
    async with module.session() as s:
        if who.has("reports.publish"):
            n = sum(1 for d, _ in await definitions(s, drafts=True) if d.draft_config)
            out.append({"title": f"{n} report draft{'s' if n != 1 else ''} to publish",
                        "url": "/reports/", "count": n})  # fmt: skip
            n = len(await dataset_proposals(s))
            out.append({"title": f"{n} proposed dataset{'s' if n != 1 else ''} to create",
                        "url": "/reports/datasets#proposals", "count": n})  # fmt: skip
            n = len(await proposals(s))
            out.append({"title": f"{n} set{'s' if n != 1 else ''} of proposed rows to add",
                        "url": "/reports/", "count": n})  # fmt: skip
    return out
