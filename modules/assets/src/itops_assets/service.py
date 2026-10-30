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

**Restricted types** (ADR 0012): an asset type is an area. Records of a type the viewer
can't see are left out of every read here (lists, lookups, links, proposals, search) and
can't be written: pages answer 404, tools say they don't exist.

Callers pass a session and commit.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from itops.sdk import Principal
from itops.sdk.content import Passage
from itops.sdk.types import TypeDef, TypeProblem, TypeView
from itops_assets import classes as cl
from itops_assets import dating
from itops_assets import devices as dv
from itops_assets import evidence as ev
from itops_assets import identity as ids
from itops_assets import readiness as rd
from itops_assets.app import module
from itops_assets.models import (
    AcceptedFinding,
    ComputerClass,
    FilledField,
    Identity,
    OsTarget,
    Proposal,
    Record,
    RecordField,
    utcnow,
)


class AssetsError(ValueError):
    """A user-facing problem (shown as a message, not a crash)."""


# ---- types ---------------------------------------------------------------------------------


async def hidden_types() -> frozenset[str]:
    """Asset types the current viewer can't see (ADR 0012); usually none."""
    return await module.areas.hidden("type")


async def available_types() -> list[TypeView]:
    """Types new records can use: published, not retired, and visible to the viewer."""
    hidden = await hidden_types()
    return [t for t in await module.types.list() if t.key not in hidden]


async def all_types() -> dict[str, TypeView]:
    """Every type the viewer can see (retired and draft-only included), for labels."""
    hidden = await hidden_types()
    return {v.key: v for v in await module.types.list(include_drafts=True) if v.key not in hidden}


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


async def _get(s: AsyncSession, record_id: str | uuid.UUID) -> Record | None:
    try:
        rid = record_id if isinstance(record_id, uuid.UUID) else uuid.UUID(str(record_id))
    except ValueError:
        return None
    return await s.get(Record, rid)


async def get(s: AsyncSession, record_id: str | uuid.UUID) -> Record | None:
    """The record, unless its type is one the viewer can't see."""
    record = await _get(s, record_id)
    return record if record is not None and record.type_key not in await hidden_types() else None


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
    if hidden := await hidden_types():
        q = q.where(Record.type_key.not_in(hidden))
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


async def filled_fields(s: AsyncSession, record_id: uuid.UUID) -> frozenset[str]:
    """Owned fields of a record that a person (or a merge) filled while the source had no
    value: they stay editable, and a sync keeps them until the source sends a value."""
    q = select(FilledField.field_key).where(FilledField.record_id == record_id)
    return frozenset((await s.execute(q)).scalars())


def _keep(
    record: Record, owned_keys: frozenset[str], name: str, fields: dict[str, Any],
    filled: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, Any]]:  # fmt: skip
    """``name``/``fields`` with every owned value put back to the record's current one. An
    owned field the source left empty can be filled in (a purchase date Lansweeper doesn't
    know), and changed again while it's ``filled``: the source's value replaces it once it
    sends one."""
    current = record_fields(record)
    if "*" in owned_keys:
        return record.name, current
    kept = dict(fields)
    for key in owned_keys - {"name"} - filled:
        if key in current:
            kept[key] = current[key]
    return (record.name if "name" in owned_keys else name), kept


async def _mark_filled(
    s: AsyncSession, record: Record, owned_keys: frozenset[str], filled: frozenset[str],
    before: dict[str, Any], after: dict[str, Any],
) -> None:  # fmt: skip
    """Remember the owned fields this change filled while the source had no value (so a sync
    keeps them), and forget the ones it cleared."""
    if "*" in owned_keys:
        return
    for key in owned_keys - {"name"} - filled:
        if _blank(before.get(key)) and not _blank(after.get(key)):
            s.add(FilledField(record_id=record.id, field_key=key))
    if cleared := [k for k in filled if _blank(after.get(k))]:
        await _unmark_filled(s, record, cleared)


async def _unmark_filled(s: AsyncSession, record: Record, keys: Sequence[str]) -> None:
    await s.execute(delete(FilledField).where(FilledField.record_id == record.id,
                                              FilledField.field_key.in_(keys)))  # fmt: skip


async def _synced_filled(
    s: AsyncSession, record: Record, fields: dict[str, Any], sparse: frozenset[str],
    filled: frozenset[str] | None = None,
) -> None:  # fmt: skip
    """After a sync: owned values the source sent win from now on; a ``sparse`` value the
    source leaves blank is kept but isn't the source's (someone cleared a wrong purchase date
    there, or another source wrote it: Lansweeper never sends HP computers' dates), so people
    can correct or clear it until the source sends one. ``filled`` None: an unchanged sync,
    which reads it only when there's something to mark."""
    have = record_fields(record)
    unsent = [k for k in sparse if _blank(fields.get(k)) and not _blank(have.get(k))]
    if filled is None:
        if not unsent:
            return
        filled = await filled_fields(s, record.id)
    if sent := [k for k in filled if not _blank(fields.get(k))]:
        await _unmark_filled(s, record, sent)
    for key in unsent:
        if key not in filled:
            s.add(FilledField(record_id=record.id, field_key=key))


async def update(
    s: AsyncSession, who: Principal, record: Record, *, name: str, fields: dict[str, Any]
) -> list[str]:
    """Replace the name and field values. Returns what changed (field keys, 'name').

    Outside a connector run, fields a connector owns keep their values: the source system
    is their source of truth (ADR 0010). Those the source left empty can be filled, and are
    remembered so its syncs keep them until it sends a value (``filled_fields``)."""
    _no_direct_ai_writes(who)
    owned_keys = await owned(record) if module.refs.run is None else frozenset[str]()
    filled = await filled_fields(s, record.id) if owned_keys else frozenset[str]()
    if owned_keys:
        name, fields = _keep(record, owned_keys, name, fields, filled)
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
    if owned_keys:
        await _mark_filled(s, record, owned_keys, filled, before, values)
    record.name = new_name
    record.fields = json.dumps(values) if values else None
    record.updated_by = who.user
    record.updated_at = utcnow()
    await s.flush()
    await _index(s, record)
    await _prune_accepted(s, record)
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


# ---- accepting a disagreement as expected ---------------------------------------------------


async def _accepted(
    s: AsyncSession, record_id: uuid.UUID | None = None
) -> dict[tuple[str, str], AcceptedFinding]:
    q = select(AcceptedFinding)
    if record_id is not None:
        q = q.where(AcceptedFinding.record_id == record_id)
    return {(str(a.record_id), a.code): a for a in (await s.execute(q)).scalars()}


def _split_accepted(
    record_id: str, found: list[ev.Finding], accepted: dict[tuple[str, str], AcceptedFinding]
) -> tuple[list[ev.Finding], list[dict[str, Any]]]:
    """(findings still open, those a person accepted: {code, text, reason, by, at})."""
    open_: list[ev.Finding] = []
    taken: list[dict[str, Any]] = []
    for f in found:
        a = accepted.get((record_id, f.code))
        if a is not None and a.facts == f.facts:
            taken.append({"code": f.code, "text": f.text, "reason": a.reason,
                          "by": a.accepted_by, "at": a.accepted_at})  # fmt: skip
        else:
            open_.append(f)
    return open_, taken


async def _synced(s: AsyncSession) -> dict[str, bool]:
    """Do AD and Sophos fill Assets (some device has an AD account, a Sophos last seen)? Only
    then does a PC without one mean it isn't in AD, or in Sophos: ``judge``'s keywords."""
    out: dict[str, bool] = {}
    for arg, key in (("ad_synced", "ad_account"), ("sophos_synced", "sophos_last_seen")):
        q = select(RecordField.record_id).where(RecordField.field_key == key).limit(1)
        out[arg] = (await s.execute(q)).first() is not None
    return out


async def findings(record: Record) -> list[ev.Finding]:
    """What doesn't line up for one device, as ``fleet`` judges it: its own sources and status,
    plus another record holding its serial or another machine its name."""
    values = record_fields(record)
    async with module.session() as s:
        found = ev.judge(values, dt.date.today(), **await _synced(s)).findings
        if not ids.serial(values.get("serial_number")) or values.get("status") in ev.GONE:
            return found
        rows = await s.execute(
            select(RecordField.record_id, RecordField.field_key, RecordField.value_text,
                   Record.name)
            .join(Record, Record.id == RecordField.record_id)
            .where(Record.type_key == record.type_key,
                   RecordField.field_key.in_(("serial_number", "status", "kind")))
        )  # fmt: skip
        devices: dict[str, dict[str, Any]] = {}
        for record_id, key, value, name in rows:
            devices.setdefault(str(record_id), {"id": str(record_id), "name": name})[key] = value
    if others := ev.same_serial(list(devices.values())).get(str(record.id)):
        found.append(ev.twin(others))
    if others := ev.same_name(list(devices.values())).get(str(record.id)):
        found.append(ev.namesake(others))
    return found


async def device_findings(record: Record) -> tuple[list[ev.Finding], list[dict[str, Any]]]:
    """(open findings, accepted ones) for one device's page."""
    found = await findings(record)
    async with module.session() as s:
        accepted = await _accepted(s, record.id)
    return _split_accepted(str(record.id), found, accepted)


async def accept_finding(
    s: AsyncSession, who: Principal, record: Record, code: str, reason: str
) -> None:
    """Accept one of a device's disagreements as expected (a person, with a reason): hidden
    from the lists, Budget and the device page while its facts stay the same."""
    _no_direct_ai_writes(who)
    reason = " ".join(reason.split())[:500]
    if not reason:
        raise AssetsError("Say why it's expected, for whoever looks at it next.")
    finding = next((f for f in await findings(record) if f.code == code), None)
    if finding is None:
        raise AssetsError("That doesn't apply to this device any more: nothing to accept.")
    current = await s.get(AcceptedFinding, (record.id, code))
    if current is None:
        current = AcceptedFinding(record_id=record.id, code=code)
        s.add(current)
    current.facts, current.reason = finding.facts, reason
    current.accepted_by, current.accepted_at = who.user, utcnow()
    await _audit(s, record, "accepted", f"Accepted as expected: {finding.text} Why: {reason}",
                 code=code)  # fmt: skip


async def unaccept_finding(s: AsyncSession, who: Principal, record: Record, code: str) -> None:
    """Show an accepted disagreement again."""
    _no_direct_ai_writes(who)
    current = await s.get(AcceptedFinding, (record.id, code))
    if current is None:
        return
    await s.delete(current)
    await _audit(s, record, "unaccepted", "Showing a disagreement again that was accepted "
                 f"({current.reason})", code=code)  # fmt: skip


async def _prune_accepted(s: AsyncSession, record: Record) -> None:
    """After a change: an acceptance whose finding went away or changed facts no longer
    holds (it shows again if the disagreement comes back)."""
    judged = ev.judge(record_fields(record), dt.date.today(), **await _synced(s))
    current = {f.code: f.facts for f in judged.findings}
    for (_, code), a in (await _accepted(s, record.id)).items():
        if code not in ev.ACROSS_DEVICES and current.get(code) != a.facts:
            await s.delete(a)


# ---- merging two records that are one thing ---------------------------------------------

NOT_MERGED = ("status",)  # the kept record's own: a duplicate's status says nothing about it


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == []


async def merge_plan(record: Record, into: Record) -> list[str]:
    """Check that ``record`` can be merged into ``into`` and return the fields it would fill
    there (those ``into`` has empty). Raises AssetsError with the reason when it can't."""
    if record.id == into.id:
        raise AssetsError("Choose another record to merge it into.")
    if record.type_key != into.type_key:
        raise AssetsError("Only records of the same type can be merged.")
    link = f"{module.name}:{record.type_key}:{record.id}"
    elsewhere = [r for r in await module.links.related(link) if r["module"] != module.name]
    if elsewhere:
        raise AssetsError(
            f"{elsewhere[0]['name']} ({elsewhere[0]['module']}) links to {record.name}: point "
            f"it at {into.name} first."
        )
    # Only references the source still has can clash: one flagged missing (the source
    # replaced or dropped that record) is forgotten by the merge.
    theirs = {(x.connector, x.source_type) for x in await module.refs.links(
        into.type_key, str(into.id)) if x.missing_since is None}  # fmt: skip
    for x in await module.refs.links(record.type_key, str(record.id)):
        if x.missing_since is None and (x.connector, x.source_type) in theirs:
            raise AssetsError(
                f"Both are synced from {x.system}, as two records there: merged, its next "
                f"run would overwrite one with the other. Fix it in {x.system} first."
            )
    have = record_fields(into)
    return sorted(
        k for k, v in record_fields(record).items()
        if k not in NOT_MERGED and not _blank(v) and _blank(have.get(k))
    )  # fmt: skip


async def merge(s: AsyncSession, who: Principal, record: Record, into: Record) -> list[str]:
    """Merge ``record`` into ``into`` (human-only): blanks in ``into`` are filled from it,
    its source references and the assets linking to it move over, and it's deleted (audited
    with its last values). Nothing is retired, so nothing syncs to AD. Returns the fields
    filled."""
    _require_human(who)
    # Every read that opens its own session comes before the first write in ``s``: those
    # reads would wait on its uncommitted rows.
    fill = await merge_plan(record, into)
    values = record_fields(record)
    link = f"{module.name}:{record.type_key}:{record.id}"
    linking = [await _get(s, r["id"]) for r in await related(link, limit=200)]
    for other in linking:
        if other is not None and other.id not in (record.id, into.id):
            repointed = {k: _repoint(v, str(record.id), str(into.id))
                         for k, v in record_fields(other).items()}  # fmt: skip
            await update(s, who, other, name=other.name, fields=repointed)
    filled: list[str] = []
    if fill:
        gained = {**record_fields(into), **{k: values[k] for k in fill}}
        filled = await update(s, who, into, name=into.name, fields=gained)
    moved = await module.refs.move(s, record.type_key, str(record.id), str(into.id))
    waiting = select(Proposal).where(
        Proposal.status == "pending",
        or_(Proposal.record_id == record.id, Proposal.merge_into == record.id),
    )
    for p in (await s.execute(waiting)).scalars():
        p.status, p.decided_by, p.decided_at = "rejected", who.user, utcnow()
    summary = f"Merged {record.name} into this record"
    if filled:
        summary += f" (filled {', '.join(filled)})"
    await _audit(s, into, "merged", summary, merged=str(record.id), filled=filled,
                 sources_moved=moved)  # fmt: skip
    await _audit(s, record, "deleted", f"Deleted {record.name}: merged into {into.name}",
                 kept=str(into.id), last_values=values)  # fmt: skip
    await module.search.remove(s, kind="record", entity_id=str(record.id))
    module.publish(
        "record_deleted", {"id": str(record.id), "type": record.type_key, "name": record.name}
    )
    await s.delete(record)
    return filled


async def merge_candidates(s: AsyncSession, record: Record) -> list[Record]:
    """Other records of its type that share a serial, MAC or host name with it: the ones it
    may be a copy of."""
    mine = select(Identity.kind, Identity.value).where(Identity.record_id == record.id)
    found: dict[uuid.UUID, Record] = {}
    for kind, value in (await s.execute(mine)).all():
        for r in await _candidates(s, record.type_key, kind, [value]):
            if r.id != record.id:
                found[r.id] = r
    return sorted(found.values(), key=lambda r: (r.name.lower(), str(r.id)))


# ---- indexing --------------------------------------------------------------------------


def value_text(value: Any) -> str:
    """A stored value as text (links are module:kind:id)."""
    if isinstance(value, dict):
        d: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return ":".join(str(d.get(k, "")) for k in ("module", "kind", "id"))
    return str(value)


# 2: identities (serial, MACs, host name) for matching across sources (ADR 0017).
module.search.version = 2


async def _index(s: AsyncSession, record: Record) -> None:
    """Rebuild the queryable rows, the identities and the search entry for one record."""
    await s.execute(delete(RecordField).where(RecordField.record_id == record.id))
    await s.execute(delete(Identity).where(Identity.record_id == record.id))
    found = ids.of_values(record.name, record_fields(record), by_name=record.type_key == "device")
    for kind, value in sorted(found):
        s.add(Identity(record_id=record.id, kind=kind, value=value[:200]))
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
            elif f.kind in ("number", "money"):
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
        area=("type", record.type_key),
    )


@module.search.reindexer
async def reindex_all() -> int:
    """Rebuild every record's search entry and queryable values (``itops search rebuild``)."""
    count = 0
    async with module.session() as s:
        for record in list((await s.execute(select(Record))).scalars()):
            await _index(s, record)
            await s.commit()  # one at a time: never one long transaction
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
    hidden = await hidden_types()
    async with module.session() as s:
        if ids is not None:
            found = [r for i in ids if (r := await _get(s, i)) is not None]
        else:
            found = await records(s, type_key=kind, query=query, limit=max(1, min(limit, 500)))
    # By id (a link someone made): one of a type the viewer can't see shows as "Restricted".
    return [{"id": str(r.id), "kind": r.type_key, "restricted": "1"} if r.type_key in hidden
            else {"id": str(r.id), "kind": r.type_key, "name": r.name, "url": f"/assets/r/{r.id}"}
            for r in found]  # fmt: skip


@module.service("related")
async def related(link: str, limit: int = 50) -> list[dict[str, str]]:
    """Assets whose fields link to ``link`` ("module:kind:id", e.g. a document or another
    asset). Each: {id, kind, name, url, field} (field = the label of the linking field)."""
    q = (
        select(Record, RecordField.field_key)
        .join(RecordField, RecordField.record_id == Record.id)
        .where(RecordField.kind == "link", RecordField.value_text == link)
        .order_by(Record.name)
        .limit(max(1, min(limit, 200)))
    )
    if hidden := await hidden_types():
        q = q.where(Record.type_key.not_in(hidden))
    async with module.session() as s:
        rows = (await s.execute(q)).all()
    types = await all_types()
    out: list[dict[str, str]] = []
    for r, field_key in rows:
        view = types.get(r.type_key)
        labels = {f.key: f.label for f in view.current.definition.fields} if view else {}
        field = str(labels.get(field_key, field_key))
        out.append({"id": str(r.id), "kind": r.type_key, "name": r.name,
                    "url": f"/assets/r/{r.id}", "field": field})  # fmt: skip
    return out


@module.service("describe_area")
async def describe_area(kind: str, key: str) -> dict[str, str] | None:
    """An asset type's name and its page, for the core access page (ADR 0012)."""
    view = await module.types.get(key) if kind == "type" else None
    return {"name": view.current.definition.name, "url": f"/assets/types/{key}"} if view else None


@module.service("area_entities")
async def area_entities(kind: str, key: str) -> dict[str, Any]:
    """The records of a type, as search entries (a new restriction marks them)."""
    async with module.session() as s:
        ids = await s.execute(select(Record.id).where(Record.type_key == key))
        return {"kind": "record", "ids": [str(i) for i in ids.scalars()] if kind == "type" else []}


@module.service("link_targets")
async def link_targets() -> list[dict[str, str]]:
    """What record-type link fields can point at in assets (the type editor lists these)."""
    return [{"target": "assets", "label": "Assets: any asset"}] + [
        {"target": f"assets.{t.key}", "label": f"Assets: {t.current.definition.name}"}
        for t in await available_types()
    ]


# ---- the fleet (ADR 0016: what Budget counts) -----------------------------------------------

FLEET_FIELDS = ("kind", "status", "manufacturer", "model", "operating_system", "location",
                "purchased", "warranty_expires", "in_service", "last_seen", "serial_number",
                "product_number", "ad_last_sign_in", "ad_account", "ad_joined", "ad_ou",
                "ad_status", "ad_home_ou", "computer_class", "hostname", "primary_ip",
                "sophos_last_seen", "sophos_health", "sophos_notes", "sophos_user",
                *rd.HARDWARE_FIELDS)  # fmt: skip


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def acquired_of(
    values: dict[str, Any], today: dt.date, warranty_years: int = 3
) -> tuple[str | None, str | None]:
    """(acquired date, basis): the purchase date, else the warranty end minus
    ``warranty_years``, else *In service since*, else an HP serial's build week, else when it
    joined the domain; (None, None) when nothing says."""
    purchased, warranty = _date(values.get("purchased")), _date(values.get("warranty_expires"))
    if purchased:
        return purchased.isoformat(), "purchased"
    if warranty:
        try:
            acquired = warranty.replace(year=warranty.year - warranty_years)
        except ValueError:  # 29 February
            acquired = warranty.replace(year=warranty.year - warranty_years, day=28)
        return acquired.isoformat(), "warranty"
    if in_service := _date(values.get("in_service")):
        return in_service.isoformat(), "in_service"
    if built := dating.built_from_serial(
        values.get("manufacturer"), values.get("serial_number"), values.get("processor"), today
    ):
        return built.isoformat(), "serial"
    if joined := _date(values.get("ad_joined")):
        return joined.isoformat(), "joined"
    return None, None


@module.service("fleet")
async def fleet(
    type_key: str = "device",
    *,
    warranty_years: int = 3,
    in_use_days: int = ev.IN_USE_DAYS,
    include_ignored: bool = False,
    with_sources: bool = False,
) -> list[dict[str, Any]]:
    """Every record of a type (the starter *Device* by default) with what a budget needs,
    for another module to count; records marked *Ignored* are left out (unless
    ``include_ignored``), and a type the
    viewer can't see gives []. Each: {id, name, url, kind, status, manufacturer, model,
    operating_system, location, purchased, warranty_expires, in_service, last_seen, serial_number,
    product_number, ad_last_sign_in, ad_account, ad_joined, ad_ou, ad_status, ad_home_ou,
    sophos_last_seen, sophos_health, sophos_notes, sophos_user, processor, memory_gb,
    storage_gb, tpm} (dates as ISO text), ``sources`` (with ``with_sources``: the systems
    that sync it, by their connectors' names; else []), plus
    ``acquired`` and ``age_basis``: the purchase date ("purchased"), else the warranty end
    minus ``warranty_years`` ("warranty"), else a person's estimate in *In service since*
    ("in_service"), else the build week in an HP serial number ("serial", an estimate:
    ``dating``), else when it joined the domain ("joined", a rough estimate: a rejoin makes
    it later), else None; ``os_ready`` (ADR 0019):
    [{target, status, why}] for each OS target that replaces the system it runs (status
    ready | not_ready | unknown); ``computer_class`` and ``class_basis`` (``classes``: a
    person's choice "set", else by model "model", else None and ""); and ``seen_by`` /
    ``disagreements`` (``evidence``): whether
    the device itself ("network": a scan or Sophos) or only AD ("ad") saw it within
    ``in_use_days``, the device trusted over AD, and sentences where the sources or its status
    don't line up; ``seen_on`` / ``seen_via``: when the device itself was last seen, and by
    what ("a scan" | "Sophos")."""
    if type_key in await hidden_types():
        return []
    targets = await os_target_list()
    rules = await class_rules()
    systems = await module.refs.systems(type_key) if with_sources else {}
    today = dt.date.today()
    async with module.session() as s:
        synced = await _synced(s)
        found = (await s.execute(select(Record).where(Record.type_key == type_key))).scalars()
        out: list[dict[str, Any]] = []
        for r in found:
            values = record_fields(r)
            if values.get("status") == "Ignored" and not include_ignored:
                continue
            item: dict[str, Any] = {"id": str(r.id), "name": r.name, "url": f"/assets/r/{r.id}",
                                    "sources": systems.get(str(r.id), [])}  # fmt: skip
            item |= {k: values.get(k) or None for k in FLEET_FIELDS}
            item["acquired"], item["age_basis"] = acquired_of(values, today, warranty_years)
            item["os_ready"] = [r.as_dict() for r in rd.readiness(targets, values)]
            item["computer_class"], item["class_basis"] = cl.classify(values, rules)
            judged = ev.judge(values, today, in_use_days, **synced)
            item["seen_by"], item["findings"] = judged.seen_by, judged.findings
            item["seen_on"], item["seen_via"] = judged.seen_on or None, judged.seen_via
            out.append(item)
        accepted = await _accepted(s)
    twins, namesakes = ev.same_serial(out), ev.same_name(out)
    for item in out:
        said: list[ev.Finding] = item.pop("findings")
        if others := twins.get(item["id"]):
            said.append(ev.twin(others))
        if others := namesakes.get(item["id"]):
            said.append(ev.namesake(others))
        open_, item["accepted"] = _split_accepted(item["id"], said, accepted)
        item["disagreements"] = [f.text for f in open_]
    return sorted(out, key=lambda i: str(i["name"]).lower())


async def set_devices(
    s: AsyncSession, who: Principal, ids: list[str], *, status: str = "", computer_class: str = ""
) -> int:
    """Set the status and/or class of several devices at once (the devices list's bulk
    change): each an ordinary edit by ``who``, audited; a status syncs to AD like any other
    (ADR 0024). Returns how many changed."""
    _no_direct_ai_writes(who)
    if not status and not computer_class:
        raise AssetsError("Choose a status or a class to set.")
    if status and status not in dv.STATUSES:
        raise AssetsError(f"Status is one of {', '.join(dv.STATUSES)}.")
    if computer_class and computer_class not in (*cl.CLASSES, "by model"):
        raise AssetsError(f"Class is one of {', '.join(cl.CLASSES)}, or by model.")
    changed = 0
    for record_id in ids:
        record = await get(s, record_id)
        if record is None or record.type_key != dv.TYPE:
            continue
        values = record_fields(record)
        if status:
            values["status"] = status
        if computer_class == "by model":
            values.pop("computer_class", None)
        elif computer_class:
            values["computer_class"] = computer_class
        changed += bool(await update(s, who, record, name=record.name, fields=values))
    return changed


async def disagreements(in_use_days: int = ev.IN_USE_DAYS) -> list[dict[str, Any]]:
    """Devices whose sources (a scan, AD) or status don't line up, with why: for people to
    fix in Assets. The same check Budget relies on."""
    return [d for d in await fleet(in_use_days=in_use_days) if d["disagreements"]]


# ---- OS readiness (ADR 0019) ---------------------------------------------------------------

READINESS_TYPE = "device"  # the records checked: the starter Device type
NOT_COUNTED = ("Retired", "Ignored")  # left out of readiness reports


def target_of(t: OsTarget) -> rd.Target:
    return rd.Target(
        name=t.name,
        upgrades_from=[str(x) for x in json.loads(t.upgrades_from or "[]")],
        processors=rd.pattern_lines(t.processors or ""),
        min_memory_gb=t.min_memory_gb,
        min_storage_gb=t.min_storage_gb,
        min_tpm=t.min_tpm,
        id=t.id,
    )


# ---- computer classes ------------------------------------------------------------------------


async def class_texts(s: AsyncSession) -> dict[str, str]:
    """Each tier's model patterns as typed (the preset where nobody has saved any)."""
    saved = {c.tier: c.models for c in (await s.execute(select(ComputerClass))).scalars()}
    return {t: saved.get(t, cl.PRESETS[t]) for t in cl.TIERS}


async def class_rules() -> cl.Rules:
    async with module.session() as s:
        return cl.Rules.from_text(await class_texts(s))


async def save_class_rules(s: AsyncSession, who: Principal, texts: dict[str, str]) -> None:
    """Change the model patterns of the tiers. Admins only (``assets.classes``, human-only);
    audited."""
    _require_human(who)
    for tier in cl.TIERS:
        bad = rd.bad_patterns(rd.pattern_lines(texts.get(tier, "")))
        if bad:
            raise AssetsError(f"These {tier} patterns aren't valid: {'; '.join(bad[:3])}")
    before = await class_texts(s)
    for tier in cl.TIERS:
        row = await s.get(ComputerClass, tier)
        if row is None:
            row = ComputerClass(tier=tier, updated_by=who.user)
            s.add(row)
        row.models = texts.get(tier, "").strip()
        row.updated_by, row.updated_at = who.user, utcnow()
    await s.flush()
    await module.audit.record(
        s, "computer_classes.saved", kind="computer_classes", entity_id="models",
        title="Computer classes", summary="Changed which models are Engineering or Standard",
        details={"before": before, "after": {t: texts.get(t, "") for t in cl.TIERS}},
    )  # fmt: skip


async def os_targets(s: AsyncSession) -> list[OsTarget]:
    return list((await s.execute(select(OsTarget).order_by(OsTarget.name))).scalars())


async def os_target_list() -> list[rd.Target]:
    async with module.session() as s:
        return [target_of(t) for t in await os_targets(s)]


def _number_or_none(text: str, label: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        n = float(text)
    except ValueError:
        raise AssetsError(f"{label} is a number of GB, e.g. 4.") from None
    if n < 0:
        raise AssetsError(f"{label} can't be negative.")
    return n or None


def _names(text: str) -> list[str]:
    out: list[str] = []
    for part in text.replace("\n", ",").split(","):
        part = " ".join(part.split())
        if part and part.lower() not in {x.lower() for x in out}:
            out.append(part)
    return out


async def save_target(
    s: AsyncSession, who: Principal, target: OsTarget | None, *, name: str, upgrades_from: str,
    processors: str, min_memory_gb: str = "", min_storage_gb: str = "", min_tpm: str = "",
    source: str = "", notes: str = "",
) -> OsTarget:  # fmt: skip
    """Add (``target`` None) or change an OS target. Admins only (``assets.targets``,
    human-only); audited."""
    _require_human(who)
    name = " ".join(name.split())[:100]
    if not name:
        raise AssetsError("Give the operating system a name, e.g. Windows 11.")
    taken = await s.scalar(select(OsTarget.id).where(OsTarget.name == name))
    if taken is not None and (target is None or taken != target.id):
        raise AssetsError(f"There's already a target called {name}.")
    systems = _names(upgrades_from)
    if not systems:
        raise AssetsError("Say which systems it replaces, e.g. Windows 10.")
    patterns = rd.pattern_lines(processors)
    bad = rd.bad_patterns(patterns)
    if bad:
        raise AssetsError(f"These processor patterns aren't valid: {'; '.join(bad[:3])}")
    memory = _number_or_none(min_memory_gb, "Minimum memory")
    storage = _number_or_none(min_storage_gb, "Minimum storage")
    tpm = min_tpm.strip() or None
    if tpm and not re.fullmatch(r"\d+(?:\.\d+)?", tpm):
        raise AssetsError("Minimum TPM is a version, e.g. 2.0.")
    if not (patterns or memory or storage or tpm):
        raise AssetsError("Give at least one requirement: processors, memory, storage or TPM.")
    before = target_of(target).__dict__ if target else None
    if target is None:
        target = OsTarget(name=name, updated_by=who.user)
        s.add(target)
    target.name = name
    target.upgrades_from = json.dumps(systems)
    target.processors = processors.strip()
    target.min_memory_gb, target.min_storage_gb, target.min_tpm = memory, storage, tpm
    target.source = source.strip()[:500]
    target.notes = notes.strip()[:2000]
    target.updated_by, target.updated_at = who.user, utcnow()
    await s.flush()
    await module.audit.record(
        s, "os_target.saved", kind="os_target", entity_id=str(target.id), title=name,
        summary=f"{'Changed' if before else 'Added'} the OS target {name}",
        details={"before": before, "after": target_of(target).__dict__},
    )  # fmt: skip
    return target


async def add_preset(s: AsyncSession, who: Principal, name: str) -> OsTarget:
    """Add an OS target from ``readiness.PRESETS`` (e.g. Windows 11)."""
    p = rd.PRESETS.get(name)
    if p is None:
        raise AssetsError(f"There's no preset called {name}.")
    return await save_target(
        s, who, None, name=name, upgrades_from=", ".join(p["upgrades_from"]),
        processors=p["processors"], min_memory_gb=str(p["min_memory_gb"] or ""),
        min_storage_gb=str(p["min_storage_gb"] or ""), min_tpm=p["min_tpm"] or "",
        source=p["source"], notes=p["notes"],
    )  # fmt: skip


async def delete_target(s: AsyncSession, who: Principal, target: OsTarget) -> None:
    _require_human(who)
    await module.audit.record(
        s, "os_target.deleted", kind="os_target", entity_id=str(target.id), title=target.name,
        summary=f"Removed the OS target {target.name}",
        details={"last": target_of(target).__dict__},
    )  # fmt: skip
    await s.delete(target)


async def readiness_report() -> list[dict[str, Any]]:
    """Every OS target with the devices it applies to (not Retired or Ignored), each with
    its hardware and readiness: [{id, name, counts {ready, not_ready, unknown}, devices:
    [{id, name, url, status, model, operating_system, processor, memory_gb, storage_gb, tpm,
    ready, why}]}]. Empty when the Device type is hidden from the viewer."""
    if READINESS_TYPE in await hidden_types():
        return []
    async with module.session() as s:
        targets = [target_of(t) for t in await os_targets(s)]
        found = list(
            (await s.execute(select(Record).where(Record.type_key == READINESS_TYPE))).scalars()
        )
    out: list[dict[str, Any]] = []
    for t in targets:
        devices: list[dict[str, Any]] = []
        for r in found:
            values = record_fields(r)
            if values.get("status") in NOT_COUNTED or not rd.applies(
                t, str(values.get("operating_system") or "")
            ):
                continue
            result = rd.check(t, values)
            devices.append(
                {
                    "id": str(r.id),
                    "name": r.name,
                    "url": f"/assets/r/{r.id}",
                    "status": values.get("status") or "",
                    "model": values.get("model") or "",
                    "operating_system": values.get("operating_system") or "",
                    "processor": rd.clean_processor(str(values.get("processor") or "")),
                    **{k: values.get(k) for k in ("memory_gb", "storage_gb", "tpm")},
                    "ready": result.status,
                    "why": result.why,
                }
            )
        devices.sort(key=lambda d: str(d["name"]).lower())
        counts = {k: sum(1 for d in devices if d["ready"] == k) for k in rd.STATUS_LABELS}
        out.append({"id": t.id, "name": t.name, "counts": counts, "devices": devices})
    return out


@module.service("dashboards")
async def dashboards() -> list[dict[str, str]]:
    """Assets' dashboards, for the Reports page (only for people who may see them)."""
    p = module.current_principal()
    if p is None or not p.has("assets.read"):
        return []
    return [
        {
            "title": "OS readiness",
            "url": "/assets/readiness",
            "description": "Which devices' hardware can run the next operating system "
            "(Windows 11 and later), and why not.",
        }
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
    *, source_type: str, source_id: str, type_key: str, name: str, fields: dict[str, Any],
    duplicates: Sequence[str] = (), identity: dict[str, Any] | None = None,
    sparse: Sequence[str] = (), shared: dict[str, Any] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Create or update the asset a source record maps to. For connector runs only.

    The first time, the record is created with every value given. After that only the
    fields the connector owns on this type are updated (people own the rest), and an
    unchanged source record is skipped. In a dry run nothing is written; the outcome says
    what would happen. Returns {outcome: created|updated|unchanged|failed, id, changed,
    problem, merged, kept}.

    ``duplicates`` are other source ids (same source type) for the same thing, when the
    source holds it more than once. Records they were imported as are merged into this
    one (``_merge_duplicates``): ``merged`` counts them, ``kept`` says which stayed and why
    (a person changed it, or another module links to it).

    ``identity`` ({serial, macs, hostname}; ADR 0017): the first time a source record is
    seen, a record of this type with the same serial, else a shared MAC, else the same host
    name (unless both have serials that differ) is linked instead of creating one: outcome
    ``matched``. Several candidates: nothing is created and the outcome is ``failed``.

    Values for fields the type doesn't have (an admin removed one, or hasn't added a new one
    yet) are left out and listed in ``ignored``: the rest still syncs.

    ``sparse``: owned fields this source only fills, never clears: a blank value keeps what
    the record has, because another source may know it (e.g. warranty dates the inventory
    doesn't have but the maker's warranty lookup does).

    ``shared``: values for fields the connector doesn't own but keeps in step both ways
    (a computer's status with AD, ADR 0024): written now, and people can still change them.
    The record's activity shows the connector made the change."""
    try:
        labels = {f.key: f.label for f in (await type_definition(type_key)).fields}
    except AssetsError as exc:
        return {"outcome": "failed", "problem": str(exc)}
    known = set(labels)
    ignored = sorted(k for k in fields if k not in known)
    fields = {k: v for k, v in fields.items() if k in known}
    result = await _upsert(source_type=source_type, source_id=source_id, type_key=type_key,
                           name=name, fields=fields, identity=identity,
                           sparse=frozenset(sparse))  # fmt: skip
    if ignored and result["outcome"] != "failed":
        result["ignored"] = ignored
    if duplicates and result["outcome"] != "failed":
        merged, kept = await _merge_duplicates(source_type, duplicates, type_key, result.get("id"))
        result |= {"merged": merged, "kept": kept}
    shared = {k: v for k, v in (shared or {}).items() if k in known}
    if shared and result["outcome"] != "failed" and result.get("id"):
        changed = await _set_shared(str(result["id"]), shared)
        if changed:
            result["changed"] = sorted({*(result.get("changed") or []), *changed})
            if result["outcome"] == "unchanged":
                result["outcome"] = "updated"
    # For the run's page: the record, and what changed in people's words.
    if result.get("id"):
        result["url"] = f"/assets/r/{result['id']}"
    if result["outcome"] in ("updated", "matched") and (changed := result.get("changed")):
        names = [labels.get(k, "Name" if k == "name" else k) for k in changed]
        result["detail"] = ", ".join(names)
    return result


async def _set_shared(record_id: str, values: dict[str, Any]) -> list[str]:
    """Write fields a connector keeps in step without owning them; what changed."""
    run = module.refs.run
    who = module.current_principal()
    assert run is not None and who is not None
    async with module.session() as s:
        record = await get(s, record_id)
        if record is None:
            return []
        current = record_fields(record)
        changed = [k for k, v in values.items() if current.get(k) != v]
        if changed and not run.dry_run:
            await update(s, who, record, name=record.name, fields=current | values)
            await s.commit()
        return changed


async def _upsert(
    *, source_type: str, source_id: str, type_key: str, name: str, fields: dict[str, Any],
    identity: dict[str, Any] | None, sparse: frozenset[str] = frozenset(),
) -> dict[str, Any]:  # fmt: skip
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
                problem = (
                    f"Not synced: it was imported as a {record.type_key} before, and the "
                    f"source now says it's a {type_key}. If it really is a {type_key} now, "
                    "delete the old record and the next run creates it again."
                )
                return {"outcome": "failed", "id": str(record.id), "problem": problem,
                        "problem_url": f"/assets/r/{record.id}"}  # fmt: skip
            if ref is not None and record is not None and ref.fingerprint == fp:
                if not run.dry_run:
                    await module.refs.seen(s, ref, fingerprint=fp)
                    await _synced_filled(s, record, fields, sparse)
                    await s.commit()
                return {"outcome": "unchanged", "id": str(record.id)}
            note: str | None = None
            at: dict[str, str] = {}
            if record is None and identity:
                found, note, decided, url = await _match(s, type_key, identity, run.connector,
                                                         source_type)  # fmt: skip
                at = {"problem_url": url} if url else {}
                if not decided:
                    return {"outcome": "failed", "problem": note, **at}
                if found is not None:
                    return await _link_match(s, who, found, owned_keys, source_type, source_id,
                                             name, fields, fp, sparse)  # fmt: skip
            if record is None:
                if run.dry_run:
                    await _check_type_available(type_key)
                    await _validated(type_key, fields)
                    _clean_name(name)
                    return {"outcome": "created", "id": None,
                            **({"problem": note, **at} if note else {})}  # fmt: skip
                record = await create(s, who, type_key=type_key, name=name, fields=fields)
                await module.refs.link(s, source_type, source_id, kind=type_key,
                                       entity_id=str(record.id), fingerprint=fp)  # fmt: skip
                await s.commit()
                return {"outcome": "created", "id": str(record.id),
                        **({"problem": note, **at} if note else {})}  # fmt: skip
            filled = await filled_fields(s, record.id)
            new_name, new_fields = _source_values(record, owned_keys, name, fields, sparse,
                                                  filled)  # fmt: skip
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
            await _synced_filled(s, record, fields, sparse, filled)
            assert ref is not None
            await module.refs.seen(s, ref, fingerprint=fp)
            await s.commit()
            return {"outcome": "updated" if changed else "unchanged", "id": str(record.id),
                    "changed": changed}  # fmt: skip
    except AssetsError as exc:
        return {"outcome": "failed", "problem": str(exc)}


def _repoint(value: Any, old: str, new: str) -> Any:
    """``value`` with links to record ``old`` pointing at record ``new``."""
    if isinstance(value, list):
        items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        return [_repoint(v, old, new) for v in items]
    if isinstance(value, dict):
        d: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        if d.get("module") == module.name and d.get("id") == old:
            return {**d, "id": new}
    found: Any = value  # pyright: ignore[reportUnknownVariableType]
    return found


async def _merge_duplicates(
    source_type: str, source_ids: Sequence[str], type_key: str, into: str | None
) -> tuple[int, list[str]]:
    """Merge the records ``source_ids`` were imported as into record ``into``: their
    references (every system's) now point at it, assets that linked to them link to it, and
    they're deleted
    (audited with their last values). Only records nobody but connectors ever changed and
    nothing outside Assets links to: others are kept and named, for a person to delete
    (they're flagged as no longer in the source). A dry run counts what would happen."""
    run = module.refs.run
    assert run is not None
    who = module.current_principal()
    assert who is not None
    merged, kept = 0, list[str]()
    async with module.session() as s:
        for source_id in source_ids:
            ref = await module.refs.find(s, source_type, str(source_id))
            if ref is None:
                continue
            other = None if ref.entity_id == into else await _get(s, ref.entity_id)
            if other is None or other.type_key != type_key:
                # Merged before (or its record is gone): it's still in the source, so it must
                # count as seen, or the run's end flags it "no longer in" the source.
                if not run.dry_run and into is not None:
                    if ref.entity_id == into:
                        await module.refs.seen(s, ref, fingerprint=None)
                    else:
                        await module.refs.link(s, source_type, str(source_id), kind=type_key,
                                               entity_id=into, fingerprint=None)  # fmt: skip
                    await s.commit()
                continue
            link = f"{module.name}:{type_key}:{other.id}"
            people = not (other.created_by.startswith("connector:")
                          and other.updated_by.startswith("connector:"))  # fmt: skip
            linked = [] if people else await module.links.related(link)
            if people or linked:
                why = ("a person changed it" if people else
                       f"{linked[0]['name']} ({linked[0]['module']}) links to it")  # fmt: skip
                kept.append(f"{other.name}: {why}")
                continue
            merged += 1
            if run.dry_run or into is None:
                continue
            # Other systems' references to it follow too (a scan matched it by serial), with
            # the fields they own: their next sync skips an unchanged source record.
            # (Read before this session writes: those reads use sessions of their own.)
            theirs = await module.refs.owned_fields(type_key, str(other.id))
            theirs -= module.refs.owns(type_key) or frozenset()
            pointing = await related(link, limit=200)
            await module.refs.link(s, source_type, str(source_id), kind=type_key,
                                   entity_id=into, fingerprint=None)  # fmt: skip
            await module.refs.move(s, type_key, str(other.id), into)
            for linked in pointing:
                record = await _get(s, linked["id"])
                if record is not None and record.id != other.id:
                    values = {k: _repoint(v, str(other.id), into)
                              for k, v in record_fields(record).items()}  # fmt: skip
                    await update(s, who, record, name=record.name, fields=values)
            kept_record = await _get(s, into)
            if kept_record is not None:
                values = record_fields(other)
                bring = {k: values[k] for k in theirs if not _blank(values.get(k))}
                await update(s, who, kept_record, name=kept_record.name,
                             fields=record_fields(kept_record) | bring)  # fmt: skip
                # A value people may change there (not the source's) stays changeable.
                had = await filled_fields(s, kept_record.id)
                for key in (await filled_fields(s, other.id)) & set(bring) - had:
                    s.add(FilledField(record_id=kept_record.id, field_key=key))
            await _audit(s, other, "deleted", f"Deleted {other.name}: the source held it "
                         "more than once; merged into the record kept", kept=into,
                         last_values=record_fields(other))  # fmt: skip
            await module.search.remove(s, kind="record", entity_id=str(other.id))
            module.publish("record_deleted", {"id": str(other.id), "type": other.type_key,
                                              "name": other.name})  # fmt: skip
            await s.delete(other)
            await s.commit()
    return merged, kept


async def _candidates(
    s: AsyncSession, type_key: str, kind: str, values: Sequence[str]
) -> list[Record]:
    if not values:
        return []
    q = (
        select(Record)
        .join(Identity, Identity.record_id == Record.id)
        .where(Record.type_key == type_key, Identity.kind == kind, Identity.value.in_(values))
        .distinct()
    )
    return list((await s.execute(q)).scalars())


async def _serials(s: AsyncSession, record: Record) -> set[str]:
    q = select(Identity.value).where(Identity.record_id == record.id, Identity.kind == "serial")
    return set((await s.execute(q)).scalars())


async def _match(
    s: AsyncSession,
    type_key: str,
    identity: dict[str, Any],
    connector: str,
    source_type: str = "",
) -> tuple[Record | None, str | None, bool, str | None]:
    """The record a newly seen source record is (ADR 0017): (record or None, a note for the
    run, decided, the page the note is about). Not decided means several candidates: create
    nothing.

    A record this connector already syncs is taken over only by serial, and only when none
    of its source records from this connector was seen this run: the source re-created the
    machine under a new id (Lansweeper does), and ``_link_match`` releases the old one."""
    serial = ids.serial(identity.get("serial"))
    macs = ids.macs(identity.get("macs"))
    host = ids.host(identity.get("hostname"))
    what = host or serial or (macs[0] if macs else "?")

    async def usable(found: list[Record], takeover: bool = False) -> list[Record]:
        # A record this connector already syncs from another source record isn't this one:
        # linking both would make them overwrite each other every run. Unless (by serial)
        # that source record wasn't seen this run: then this one replaces it.
        out: list[Record] = []
        for r in found:
            mine = [x for x in await module.refs.links(r.type_key, str(r.id))
                    if x.connector == connector]  # fmt: skip
            replaced = takeover and all(
                x.source_type == source_type and not x.seen_this_run for x in mine
            )
            if not mine or replaced:
                out.append(r)
        return out

    def several(found: list[Record], by: str) -> tuple[None, str, bool, str]:
        names = ", ".join(sorted(r.name for r in found))
        return None, (f"{what} wasn't synced: it matches {len(found)} records by {by} "
                      f"({names}), probably one machine held twice. Merge the stale one into "
                      "the other (Merge into another record, on its page) and the next run "
                      "syncs it."), False, f"/assets/r/{found[0].id}"  # fmt: skip

    for kind, values in (("serial", [serial] if serial else []), ("mac", macs)):
        found = await usable(await _candidates(s, type_key, kind, values), kind == "serial")
        if len(found) == 1:
            return found[0], None, True, None
        if found:
            return several(found, kind)
    found = await usable(await _candidates(s, type_key, "host", [host] if host else []))
    if serial:
        reused = [r for r in found if (theirs := await _serials(s, r)) and serial not in theirs]
        found = [r for r in found if r not in reused]
        if reused and not found:
            old = reused[0]
            return None, (f"{what} has the host name of an existing device ({old.name}) but a "
                          "different serial, so it was added as a new device: probably a "
                          "replacement that kept the name. If the old one is gone, open it and "
                          "set its status to Retired."), True, f"/assets/r/{old.id}"  # fmt: skip
    if len(found) == 1:
        return found[0], None, True, None
    if found:
        return several(found, "host name")
    return None, None, True, None


async def _link_match(
    s: AsyncSession, who: Principal, record: Record, owned_keys: frozenset[str],
    source_type: str, source_id: str, name: str, fields: dict[str, Any], fp: str,
    sparse: frozenset[str] = frozenset(),
) -> dict[str, Any]:  # fmt: skip
    """Link a source record to the existing record it matched; set the fields it owns."""
    run = module.refs.run
    assert run is not None
    filled = await filled_fields(s, record.id)
    new_name, new_fields = _source_values(record, owned_keys, name, fields, sparse, filled)
    if run.dry_run:
        await _validated(record.type_key, new_fields)
        return {"outcome": "matched", "id": str(record.id)}
    # Taking over from a source record the source replaced (_match): forget the old one. Read
    # before writing anything (links() has its own session, which would wait on ours).
    replaced = [
        old for old in await module.refs.links(record.type_key, str(record.id))
        if old.connector == run.connector and old.source_type == source_type
        and old.source_id != source_id and not old.seen_this_run
    ]  # fmt: skip
    changed = await update(s, who, record, name=new_name, fields=new_fields)
    await _synced_filled(s, record, fields, sparse, filled)
    await module.refs.link(s, source_type, source_id, kind=record.type_key,
                           entity_id=str(record.id), fingerprint=fp)  # fmt: skip
    for old in replaced:
        await module.refs.release(s, old.source_type, old.source_id)
    await s.commit()
    return {"outcome": "matched", "id": str(record.id), "changed": changed}


def _source_values(
    record: Record, owned_keys: frozenset[str], name: str, fields: dict[str, Any],
    sparse: frozenset[str] = frozenset(), filled: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, Any]]:  # fmt: skip
    """The record's values with the connector's owned ones replaced by the source's. A
    ``sparse`` field left blank by the source keeps the record's value, and so does one a
    person ``filled`` while the source had none; a value the source sent before and now
    clears is cleared."""
    if "*" in owned_keys:
        return name, fields
    merged = record_fields(record)
    for key in owned_keys - {"name"}:
        if key in fields and fields[key] not in (None, "", []):
            merged[key] = fields[key]
        elif key not in sparse and key not in filled:
            merged.pop(key, None)
    return (name if "name" in owned_keys else record.name), merged


# ---- proposals (AI drafts, people approve) ------------------------------------------------


def proposal_fields(p: Proposal) -> dict[str, Any]:
    """A new record's values; for a change, only the fields it changes (None clears one)."""
    if p.changes:
        changes: dict[str, Any] = json.loads(p.changes).get("fields") or {}
        return changes
    values: dict[str, Any] = json.loads(p.fields) if p.fields else {}
    return values


def proposal_renames(p: Proposal) -> str | None:
    """The new name a proposed change gives its record, or None."""
    renamed: str | None = json.loads(p.changes).get("name") if p.changes else None
    return renamed


def whole_copy(p: Proposal) -> bool:
    """A change proposed before proposals held only what they change: it holds the whole
    record as it was then, so approving it could undo what changed since."""
    return p.record_id is not None and p.merge_into is None and p.changes is None


WHOLE_COPY = (
    "Not applied: this proposal was made before proposals held only what they change. It "
    "holds the whole record as it was when proposed, so approving it could undo changes made "
    "since (a status set by AD, say). Reject it and ask for the change again."
)


def _apply(values: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """``values`` with ``changes`` applied: a blank value clears the field."""
    out = dict(values)
    for key, value in changes.items():
        if _blank(value):
            out.pop(key, None)
        else:
            out[key] = value
    return out


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
    save, for a person to approve. For a change, ``fields`` holds only what changes (a blank
    value clears the field) and ``name`` the record's name (a different one renames it): on
    approval just that is applied to the record as it is then."""
    if record is None and not type_key:
        raise AssetsError("Say which type the new asset is.")
    tkey = record.type_key if record else str(type_key)
    if record is None:
        await _check_type_available(tkey)
    values = await _validated(tkey, _apply(record_fields(record), fields) if record else fields)
    if record is not None and (owned_keys := await owned(record)):
        filled = await filled_fields(s, record.id)
        kept_name, kept = _keep(record, owned_keys, name, values, filled)
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
    changes: dict[str, Any] | None = None
    if record is not None:
        # Only what it changes, as checked (a clear is None).
        changes = {"fields": {k: values.get(k) for k in fields}}
        if _clean_name(name) != record.name:
            changes["name"] = _clean_name(name)
    p = Proposal(
        record_id=record.id if record else None,
        type_key=tkey,
        name=_clean_name(name),
        fields=json.dumps(values) if values and record is None else None,
        changes=json.dumps(changes) if changes is not None else None,
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


async def propose_merge(
    s: AsyncSession, who: Principal, record: Record, into: Record, *, note: str
) -> Proposal:
    """Draft merging ``record`` into ``into`` (checked like the merge itself), for a person
    to approve."""
    fill = await merge_plan(record, into)
    pending = select(Proposal).where(Proposal.status == "pending",
                                     Proposal.record_id == record.id,
                                     Proposal.merge_into == into.id)  # fmt: skip
    if (await s.execute(pending)).first():
        raise AssetsError(f"Merging {record.name} into {into.name} is already proposed.")
    p = Proposal(
        record_id=record.id,
        merge_into=into.id,
        type_key=record.type_key,
        name=record.name,
        note=" ".join(note.split())[:500],
        proposed_by=who.user,
        proposed_via_ai=who.via_ai,
    )
    s.add(p)
    await s.flush()
    summary = f"Proposed merging {record.name} into {into.name}"
    if fill:
        summary += f" (would fill {', '.join(fill)})"
    if p.note:
        summary += f": {p.note}"
    await module.audit.record(
        s, "asset.proposed", kind=record.type_key, entity_id=str(record.id), title=record.name,
        summary=summary, details={"proposal": p.id, "merge_into": str(into.id)},
    )  # fmt: skip
    return p


async def pending_proposals(s: AsyncSession) -> list[Proposal]:
    q = select(Proposal).where(Proposal.status == "pending").order_by(Proposal.created_at)
    if hidden := await hidden_types():
        q = q.where(Proposal.type_key.not_in(hidden))
    return list((await s.execute(q)).scalars())


async def decide(s: AsyncSession, who: Principal, p: Proposal, *, approve: bool) -> Record | None:
    """Approve (apply as ``who``) or reject a pending proposal. Human-only."""
    _require_human(who)
    if p.type_key in await hidden_types():
        raise AssetsError("That proposal doesn't exist.")
    if p.status != "pending":
        raise AssetsError("That proposal was already decided.")
    record: Record | None = None
    if approve and p.merge_into is not None:
        merged = await get(s, p.record_id) if p.record_id else None
        record = await get(s, p.merge_into)
        if merged is None or record is None:
            raise AssetsError("One of the records it merges no longer exists.")
        await merge(s, who, merged, record)
    elif approve:
        if p.record_id is None:
            record = await create(
                s, who, type_key=p.type_key, name=p.name, fields=proposal_fields(p)
            )
        else:
            if whole_copy(p):
                raise AssetsError(WHOLE_COPY)
            record = await get(s, p.record_id)
            if record is None:
                raise AssetsError("The asset it changes no longer exists.")
            # Onto the record as it is now: what changed since the proposal stays.
            values = _apply(record_fields(record), proposal_fields(p))
            name = proposal_renames(p) or record.name
            await update(s, who, record, name=name, fields=values)
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


async def unreviewed_devices(s: AsyncSession) -> int:
    """Devices a sync found that nobody has reviewed yet (status *Unreviewed*)."""
    if dv.TYPE in await hidden_types():
        return 0
    q = (
        select(func.count())
        .select_from(RecordField)
        .join(Record, Record.id == RecordField.record_id)
        .where(Record.type_key == dv.TYPE, RecordField.field_key == "status",
               RecordField.value_text == "Unreviewed")
    )  # fmt: skip
    return int((await s.execute(q)).scalar() or 0)


@module.service("waiting")
async def waiting() -> list[dict[str, Any]]:
    """What's waiting for the person looking (core's *Waiting for you*): proposals from AI to
    approve, and devices a sync found that need a status."""
    who = module.current_principal()
    out: list[dict[str, Any]] = []
    if who is None:
        return out
    async with module.session() as s:
        if who.has("assets.publish"):
            n = len(await pending_proposals(s))
            out.append({"title": f"{n} proposed asset change{'s' if n != 1 else ''} to approve",
                        "url": "/assets/proposals", "count": n})  # fmt: skip
        if who.has("assets.write"):
            n = await unreviewed_devices(s)
            out.append({"title": f"{n} device{'s' if n != 1 else ''} found by a sync to review",
                        "url": "/assets/t/device?view=unreviewed", "count": n})  # fmt: skip
    return out
