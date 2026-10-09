"""Asset service: the one place assets change (ADR 0009).

Pages, AI tools and the IT Glue import all call these functions, so the rules live once:

* Every record has a **type** (core record types) and a **name**; its values are checked
  against the type's published definition on every save (required fields included).
* **People edit directly**; every create, change and delete is audited with what changed.
* **AI changes are proposals**: ``propose`` stores them; a person approves (applies) or
  rejects them. ``assets.publish`` is human-only, and the service refuses AI principals
  as a second lock.
* After every change the record's values are re-indexed: ``assets.record_fields``
  (queryable) and the core search index (credential fields are never searched).

Callers pass a session and commit.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal
from itops.sdk.content import Passage
from itops.sdk.types import TypeDef, TypeProblem, TypeView
from itops_assets.app import module
from itops_assets.models import Proposal, Record, RecordField, utcnow


class AssetsError(ValueError):
    """A user-facing problem (shown as a message, not a crash)."""


# ---- types ---------------------------------------------------------------------------------


async def available_types() -> list[TypeView]:
    """Types new records can use: published and not retired."""
    return await module.types.list()


async def all_types() -> dict[str, TypeView]:
    """Every type (retired and draft-only included), for labels."""
    return {v.key: v for v in await module.types.list(include_drafts=True)}


async def type_definition(type_key: str) -> TypeDef:
    view = await module.types.get(type_key)
    if view is None or view.published is None:
        raise AssetsError(f"There's no published asset type {type_key!r}.")
    return view.published.definition


def record_fields(record: Record) -> dict[str, Any]:
    if not record.fields:
        return {}
    values: dict[str, Any] = json.loads(record.fields)
    return values


def known_fields(definition: TypeDef, values: dict[str, Any]) -> dict[str, Any]:
    keys = {f.key for f in definition.fields}
    return {k: v for k, v in values.items() if k in keys}


def _clean_name(name: str) -> str:
    name = " ".join(name.split())[:300]
    if not name:
        raise AssetsError("Give it a name.")
    return name


async def _validated(type_key: str, values: dict[str, Any]) -> dict[str, Any]:
    try:
        return await module.types.validate(type_key, values, require=True)
    except TypeProblem as exc:
        raise AssetsError(str(exc)) from exc


async def _check_type_available(type_key: str) -> None:
    if type_key not in {t.key for t in await available_types()}:
        raise AssetsError(f"{type_key!r} isn't an available asset type (unknown or retired).")


def _no_direct_ai_writes(who: Principal) -> None:
    # AI drafts, people approve (ADR 0009): over REST a token holder has assets.write, but
    # an AI connection proposes (propose_record / propose_change) and a person approves.
    if who.via_ai:
        raise AssetsError(
            "An AI connection can't change assets directly: it proposes the change "
            "(assets_propose_record / assets_propose_change) and a person approves it."
        )


def _require_human(who: Principal) -> None:
    # The route/tool permission check is the real gate (assets.publish is human_only);
    # this is a second lock in case a future caller forgets it.
    if who.via_ai:
        raise AssetsError("Approving, rejecting and deleting assets is done by a person.")


# ---- reads -----------------------------------------------------------------------------


async def get(s: AsyncSession, record_id: str | uuid.UUID) -> Record | None:
    try:
        rid = record_id if isinstance(record_id, uuid.UUID) else uuid.UUID(str(record_id))
    except ValueError:
        return None
    return await s.get(Record, rid)


async def records(
    s: AsyncSession,
    *,
    type_key: str | None = None,
    query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[Record]:
    """Records by name, optionally of one type and matching ``query`` in the name or any
    field value (plain text match; the platform search does meaning)."""
    q = select(Record).order_by(Record.name).limit(limit).offset(offset)
    if type_key:
        q = q.where(Record.type_key == type_key)
    if query.strip():
        like = f"%{query.strip()}%"
        in_fields = select(RecordField.record_id).where(RecordField.value_text.like(like))
        q = q.where(or_(Record.name.like(like), Record.id.in_(in_fields)))
    return list((await s.execute(q)).scalars())


async def counts_by_type(s: AsyncSession) -> dict[str, int]:
    rows = await s.execute(select(Record.type_key, func.count()).group_by(Record.type_key))
    return {k: int(n) for k, n in rows.all()}


# ---- writes (people) -------------------------------------------------------------------


async def _audit(s: AsyncSession, record: Record, verb: str, summary: str, **details: Any) -> None:
    await module.audit.record(
        s, f"asset.{verb}", kind=record.type_key, entity_id=str(record.id), title=record.name,
        summary=summary, details=details or None,
    )  # fmt: skip


async def create(
    s: AsyncSession,
    who: Principal,
    *,
    type_key: str,
    name: str,
    fields: dict[str, Any] | None = None,
) -> Record:
    _no_direct_ai_writes(who)
    await _check_type_available(type_key)
    values = await _validated(type_key, fields or {})
    record = Record(
        type_key=type_key,
        name=_clean_name(name),
        fields=json.dumps(values) if values else None,
        created_by=who.user,
        updated_by=who.user,
    )
    s.add(record)
    await s.flush()
    await _index(s, record)
    await _audit(s, record, "created", f"Created {record.name}")
    module.publish("record_created", {"id": str(record.id), "type": type_key, "name": record.name})
    return record


async def owned(record: Record) -> frozenset[str]:
    """Fields of ``record`` a connector owns (ADR 0010): "name", field keys, or "*"."""
    return await module.refs.owned_fields(record.type_key, str(record.id))


def _keep(
    record: Record, owned_keys: frozenset[str], name: str, fields: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """``name``/``fields`` with every owned value put back to the record's current one."""
    current = record_fields(record)
    if "*" in owned_keys:
        return record.name, current
    kept = dict(fields)
    for key in owned_keys - {"name"}:
        if key in current:
            kept[key] = current[key]
        else:
            kept.pop(key, None)
    return (record.name if "name" in owned_keys else name), kept


async def update(
    s: AsyncSession, who: Principal, record: Record, *, name: str, fields: dict[str, Any]
) -> list[str]:
    """Replace the name and field values. Returns what changed (field keys, 'name').

    Outside a connector run, fields a connector owns keep their values: the source system
    is their source of truth (ADR 0010)."""
    _no_direct_ai_writes(who)
    if module.refs.run is None and (owned_keys := await owned(record)):
        name, fields = _keep(record, owned_keys, name, fields)
    new_name = _clean_name(name)
    values = await _validated(record.type_key, fields)
    before = record_fields(record)
    changed = [k for k in sorted(set(before) | set(values)) if before.get(k) != values.get(k)]
    details: dict[str, Any] = {k: [before.get(k), values.get(k)] for k in changed}
    if new_name != record.name:
        changed.insert(0, "name")
        details["name"] = [record.name, new_name]
    if not changed:
        return []
    record.name = new_name
    record.fields = json.dumps(values) if values else None
    record.updated_by = who.user
    record.updated_at = utcnow()
    await s.flush()
    await _index(s, record)
    await _audit(s, record, "updated", "Changed " + ", ".join(changed), **details)
    module.publish("record_updated", {"id": str(record.id), "type": record.type_key,
                                      "name": record.name, "changed": changed})  # fmt: skip
    return changed


async def delete_record(s: AsyncSession, who: Principal, record: Record) -> None:
    """Delete a record (human-only). Its last values stay in the audit trail."""
    _require_human(who)
    await _audit(s, record, "deleted", f"Deleted {record.name}", last_values=record_fields(record))
    await module.search.remove(s, kind="record", entity_id=str(record.id))
    module.publish(
        "record_deleted", {"id": str(record.id), "type": record.type_key, "name": record.name}
    )
    await s.delete(record)


# ---- indexing --------------------------------------------------------------------------


def value_text(value: Any) -> str:
    """A stored value as text (links are module:kind:id)."""
    if isinstance(value, dict):
        d: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return ":".join(str(d.get(k, "")) for k in ("module", "kind", "id"))
    return str(value)


async def _index(s: AsyncSession, record: Record) -> None:
    """Rebuild the queryable rows and the search entry for one record."""
    await s.execute(delete(RecordField).where(RecordField.record_id == record.id))
    try:
        definition = await type_definition(record.type_key)
    except AssetsError:
        definition = None
    values = known_fields(definition, record_fields(record)) if definition else {}
    lines: list[str] = []
    for f in definition.fields if definition else []:
        raw = values.get(f.key)
        if raw is None or raw == "" or raw == []:
            continue
        items: list[Any] = raw if isinstance(raw, list) else [raw]  # pyright: ignore[reportUnknownVariableType]
        shown = [value_text(i) for i in items]
        for pos, item in enumerate(items):
            row = RecordField(record_id=record.id, field_key=f.key, position=pos, kind=f.kind,
                              value_text=shown[pos][:450])  # fmt: skip
            if f.kind == "date":
                row.value_date = dt.date.fromisoformat(str(item))
            elif f.kind == "number":
                row.value_number = float(item)
            s.add(row)
        if f.kind not in ("credential", "link"):
            lines.append(f"{f.label}: {', '.join(shown)}")
    type_name = definition.name if definition else record.type_key
    await module.search.index(
        s,
        kind="record",
        entity_id=str(record.id),
        title=record.name,
        path=f"/r/{record.id}",
        passages=[Passage(label=type_name, anchor=None, text="\n".join([record.name, *lines]))],
        read_permission="assets.read",
    )


@module.search.reindexer
async def reindex_all() -> int:
    """Rebuild every record's search entry and queryable values (``itops search rebuild``)."""
    count = 0
    async with module.session() as s:
        for record in (await s.execute(select(Record))).scalars():
            await _index(s, record)
            count += 1
        await s.commit()
    return count


# ---- the lookup service (other modules link to assets through it) ---------------------


@module.service("lookup")
async def lookup(
    *,
    ids: list[str] | None = None,
    kind: str | None = None,
    query: str = "",
    limit: int = 50,
) -> list[dict[str, str]]:
    """Assets for link pickers and labels: by ``ids``, or by ``kind`` (type key) and an
    optional name ``query``. Each: {id, kind, name, url}."""
    async with module.session() as s:
        if ids is not None:
            found = [r for i in ids if (r := await get(s, i)) is not None]
        else:
            found = await records(s, type_key=kind, query=query, limit=max(1, min(limit, 500)))
    return [{"id": str(r.id), "kind": r.type_key, "name": r.name, "url": f"/assets/r/{r.id}"}
            for r in found]  # fmt: skip


@module.service("related")
async def related(link: str, limit: int = 50) -> list[dict[str, str]]:
    """Assets whose fields link to ``link`` ("module:kind:id", e.g. a document or another
    asset). Each: {id, kind, name, url, field} (field = the label of the linking field)."""
    async with module.session() as s:
        rows = (
            await s.execute(
                select(Record, RecordField.field_key)
                .join(RecordField, RecordField.record_id == Record.id)
                .where(RecordField.kind == "link", RecordField.value_text == link)
                .order_by(Record.name)
                .limit(max(1, min(limit, 200)))
            )
        ).all()
    types = await all_types()
    out: list[dict[str, str]] = []
    for r, field_key in rows:
        view = types.get(r.type_key)
        labels = {f.key: f.label for f in view.current.definition.fields} if view else {}
        field = str(labels.get(field_key, field_key))
        out.append({"id": str(r.id), "kind": r.type_key, "name": r.name,
                    "url": f"/assets/r/{r.id}", "field": field})  # fmt: skip
    return out


@module.service("link_targets")
async def link_targets() -> list[dict[str, str]]:
    """What record-type link fields can point at in assets (the type editor lists these)."""
    return [{"target": "assets", "label": "Assets: any asset"}] + [
        {"target": f"assets.{t.key}", "label": f"Assets: {t.current.definition.name}"}
        for t in await available_types()
    ]


# ---- retiring a type (core's types pages ask what happens to its records) ------------------


@module.service("type_usage")
async def type_usage(type_key: str) -> int:
    """How many records have this type."""
    async with module.session() as s:
        query = select(func.count()).select_from(Record).where(Record.type_key == type_key)
        return int(await s.scalar(query) or 0)


@module.service("move_type")
async def move_type(type_key: str, to: str) -> dict[str, Any]:
    """Give every record of ``type_key`` the type ``to``, keeping their stored values.
    Records a connector syncs stay (their connector owns their type). Returns {moved, kept}."""
    who = module.current_principal()
    if who is None or not who.has("assets.write") or who.via_ai:
        raise AssetsError("Only a person with assets.write can move records.")
    await _check_type_available(to)
    moved = kept = 0
    async with module.session() as s:
        found = await s.execute(select(Record).where(Record.type_key == type_key))
        for record in list(found.scalars()):
            if await owned(record):
                kept += 1
                continue
            record.type_key = to
            record.updated_by = who.user
            record.updated_at = utcnow()
            await s.flush()
            await _index(s, record)
            await _audit(s, record, "updated", f"Moved from type {type_key} to {to}",
                         type=[type_key, to])  # fmt: skip
            event = {"id": str(record.id), "type": to, "name": record.name, "changed": ["type"]}
            module.publish("record_updated", event)
            moved += 1
        await s.commit()
    return {"moved": moved, "kept": kept}


# ---- connectors write through here (ADR 0010) ---------------------------------------------


@module.service("upsert")
async def upsert(
    *, source_type: str, source_id: str, type_key: str, name: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Create or update the asset a source record maps to. For connector runs only.

    The first time, the record is created with every value given. After that only the
    fields the connector owns on this type are updated (people own the rest), and an
    unchanged source record is skipped. In a dry run nothing is written; the outcome says
    what would happen. Returns {outcome: created|updated|unchanged|failed, id, changed,
    problem}."""
    run = module.refs.run
    if run is None:
        raise AssetsError("assets.upsert is for connector runs (ADR 0010).")
    who = module.current_principal()
    if who is None or not who.has("assets.write"):
        return {"outcome": "failed", "problem": "The connector needs the assets.write permission."}
    owned_keys = module.refs.owns(type_key) or frozenset()
    if not owned_keys:
        return {"outcome": "failed", "problem": f"The connector doesn't declare which fields "
                f"it owns on assets.{type_key} ([connectors.*.owns])."}  # fmt: skip
    fp = module.refs.fingerprint({"type": type_key, "name": name, "fields": fields})
    try:
        async with module.session() as s:
            ref = await module.refs.find(s, source_type, source_id)
            record = await get(s, ref.entity_id) if ref else None
            if record is not None and record.type_key != type_key:
                problem = f"Already imported as a {record.type_key}, not a {type_key}."
                return {"outcome": "failed", "id": str(record.id), "problem": problem}
            if ref is not None and record is not None and ref.fingerprint == fp:
                if not run.dry_run:
                    await module.refs.seen(s, ref, fingerprint=fp)
                    await s.commit()
                return {"outcome": "unchanged", "id": str(record.id)}
            if record is None:
                if run.dry_run:
                    await _check_type_available(type_key)
                    await _validated(type_key, fields)
                    _clean_name(name)
                    return {"outcome": "created", "id": None}
                record = await create(s, who, type_key=type_key, name=name, fields=fields)
                await module.refs.link(s, source_type, source_id, kind=type_key,
                                       entity_id=str(record.id), fingerprint=fp)  # fmt: skip
                await s.commit()
                return {"outcome": "created", "id": str(record.id)}
            new_name, new_fields = _source_values(record, owned_keys, name, fields)
            if run.dry_run:
                values = await _validated(type_key, new_fields)
                before = record_fields(record)
                changed = [
                    k for k in sorted(set(before) | set(values)) if before.get(k) != values.get(k)
                ]
                if _clean_name(new_name) != record.name:
                    changed.insert(0, "name")
                return {"outcome": "updated" if changed else "unchanged", "id": str(record.id),
                        "changed": changed}  # fmt: skip
            changed = await update(s, who, record, name=new_name, fields=new_fields)
            assert ref is not None
            await module.refs.seen(s, ref, fingerprint=fp)
            await s.commit()
            return {"outcome": "updated" if changed else "unchanged", "id": str(record.id),
                    "changed": changed}  # fmt: skip
    except AssetsError as exc:
        return {"outcome": "failed", "problem": str(exc)}


def _source_values(
    record: Record, owned_keys: frozenset[str], name: str, fields: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """The record's values with the connector's owned ones replaced by the source's."""
    if "*" in owned_keys:
        return name, fields
    merged = record_fields(record)
    for key in owned_keys - {"name"}:
        if key in fields and fields[key] not in (None, "", []):
            merged[key] = fields[key]
        else:
            merged.pop(key, None)
    return (name if "name" in owned_keys else record.name), merged


# ---- proposals (AI drafts, people approve) ------------------------------------------------


def proposal_fields(p: Proposal) -> dict[str, Any]:
    values: dict[str, Any] = json.loads(p.fields) if p.fields else {}
    return values


async def propose(
    s: AsyncSession,
    who: Principal,
    *,
    type_key: str | None = None,
    record: Record | None = None,
    name: str,
    fields: dict[str, Any],
    note: str = "",
) -> Proposal:
    """Draft a new record (``type_key``) or a change to ``record``, checked like a real
    save, for a person to approve."""
    if record is None and not type_key:
        raise AssetsError("Say which type the new asset is.")
    tkey = record.type_key if record else str(type_key)
    if record is None:
        await _check_type_available(tkey)
    values = await _validated(tkey, fields)
    if record is not None and (owned_keys := await owned(record)):
        kept_name, kept = _keep(record, owned_keys, name, values)
        touched = [k for k in sorted(set(kept) | set(values)) if kept.get(k) != values.get(k)]
        if kept_name != _clean_name(name):
            touched.insert(0, "name")
        if touched:
            sources = ", ".join(sorted({link.system for link in await module.refs.links(
                record.type_key, str(record.id))}))  # fmt: skip
            raise AssetsError(
                f"{', '.join(touched)} {'is' if len(touched) == 1 else 'are'} synced from "
                f"{sources}; change {'it' if len(touched) == 1 else 'them'} there."
            )
    p = Proposal(
        record_id=record.id if record else None,
        type_key=tkey,
        name=_clean_name(name),
        fields=json.dumps(values) if values else None,
        note=" ".join(note.split())[:500],
        proposed_by=who.user,
        proposed_via_ai=who.via_ai,
    )
    s.add(p)
    await s.flush()
    summary = ("Proposed a change to " if record else "Proposed new asset ") + p.name
    if p.note:
        summary += f": {p.note}"
    await module.audit.record(
        s, "asset.proposed", kind=tkey, entity_id=str(record.id) if record else None,
        title=p.name, summary=summary, details={"proposal": p.id},
    )  # fmt: skip
    return p


async def pending_proposals(s: AsyncSession) -> list[Proposal]:
    rows = await s.execute(
        select(Proposal).where(Proposal.status == "pending").order_by(Proposal.created_at)
    )
    return list(rows.scalars())


async def decide(s: AsyncSession, who: Principal, p: Proposal, *, approve: bool) -> Record | None:
    """Approve (apply as ``who``) or reject a pending proposal. Human-only."""
    _require_human(who)
    if p.status != "pending":
        raise AssetsError("That proposal was already decided.")
    record: Record | None = None
    if approve:
        if p.record_id is None:
            record = await create(
                s, who, type_key=p.type_key, name=p.name, fields=proposal_fields(p)
            )
        else:
            record = await get(s, p.record_id)
            if record is None:
                raise AssetsError("The asset it changes no longer exists.")
            await update(s, who, record, name=p.name, fields=proposal_fields(p))
    p.status = "approved" if approve else "rejected"
    p.decided_by = who.user
    p.decided_at = utcnow()
    await module.audit.record(
        s, "asset.proposal_" + p.status, kind=p.type_key,
        entity_id=str(record.id if record else p.record_id) if (record or p.record_id) else None,
        title=p.name, details={"proposal": p.id},
        summary=f"{p.status.capitalize()} the proposal for {p.name} by {p.proposed_by}",
    )  # fmt: skip
    return record
