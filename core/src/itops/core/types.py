"""Record types and typed fields (ADR 0008): the core type builder.

A **type** is a named set of **fields** that an admin defines without code, e.g. Vendor =
account number, support phone, portal URL, account manager. Docs uses types for
document types; the assets module for asset kinds. Modules store their records' field
*values* themselves (versioned with their content) and use this module to define types
and to validate values.

Types follow "AI drafts, humans approve" like documents: anyone allowed to draft
(``<module>.types.draft``; an AI may) saves a draft; it has no effect until an admin
publishes it (``<module>.types.approve``, human-only). Both permissions are admin-only
by construction (roles grant by last word: read / write / publish). A module opts in by
declaring both in its manifest.

Changing a published type never loses content: field keys and kinds are fixed once
published (a value's meaning can't change under it); labels, help, options and order
can change, and new fields start empty. Removing a field hides it; stored values stay.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from itops.core import auth
from itops.core.audit import AuditLog

FieldKind = Literal["text", "longtext", "number", "date", "select", "url", "credential", "link"]
FIELD_KINDS: dict[str, str] = {
    "text": "One line of text",
    "longtext": "Several lines of text",
    "number": "A number",
    "date": "A date",
    "select": "A choice from a list (options)",
    "url": "A web address (http/https)",
    "credential": (
        "A 1Password reference (op://vault/item/field) or 1Password link, never the secret itself"
    ),
    "link": "A link to another record (a device, a document, ...). Choose what it links to below",
}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TARGET_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}(\.[a-z][a-z0-9_]{0,49})?$")
MAX_FIELDS = 60


class TypeProblem(ValueError):
    """A type definition or a value doesn't validate. The message is for people (and AI):
    tools and pages show it as is."""


# ---- definitions -------------------------------------------------------------------------


class FieldDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Stable id, lowercase_with_underscores; fixed once published")
    label: str = Field(min_length=1, max_length=100)
    kind: FieldKind
    required: bool = False
    help: str = Field(default="", max_length=300)
    options: list[str] = Field(default_factory=list[str], description="select only")
    multiple: bool = Field(default=False, description="select and link only")
    target: str | None = Field(default=None, description="link only: 'module' or 'module.kind'")

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not KEY_RE.match(v):
            raise ValueError(
                "use lowercase letters, digits and underscores, starting with a letter"
            )
        return v

    @field_validator("label", "help")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return " ".join(v.split())

    @model_validator(mode="after")
    def _kind_rules(self) -> FieldDef:
        if self.kind == "select":
            opts = [" ".join(o.split()) for o in self.options if o.strip()]
            if not opts:
                raise ValueError(f"{self.key}: a select field needs options")
            if len(set(o.lower() for o in opts)) != len(opts):
                raise ValueError(f"{self.key}: options must be unique")
            if len(opts) > 100:
                raise ValueError(f"{self.key}: at most 100 options")
            self.options = opts
        elif self.options:
            raise ValueError(f"{self.key}: only select fields have options")
        if self.kind == "link":
            if not self.target or not TARGET_RE.match(self.target):
                raise ValueError(
                    f"{self.key}: a link field needs a target like 'docs' or 'assets.device'"
                )
        elif self.target:
            raise ValueError(f"{self.key}: only link fields have a target")
        if self.multiple and self.kind not in ("select", "link"):
            raise ValueError(f"{self.key}: only select and link fields can hold several values")
        return self


class TypeDef(BaseModel):
    """What an admin (or an AI, as a draft) defines."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    fields: list[FieldDef] = Field(default_factory=list[FieldDef])

    @field_validator("name", "description")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return " ".join(v.split())

    @model_validator(mode="after")
    def _unique(self) -> TypeDef:
        if len(self.fields) > MAX_FIELDS:
            raise ValueError(f"at most {MAX_FIELDS} fields")
        keys = [f.key for f in self.fields]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"two fields have the same ID: {', '.join(dupes)}")
        return self


def parse_definition(data: TypeDef | dict[str, Any]) -> TypeDef:
    """Validate a definition; raise TypeProblem with readable messages."""
    if isinstance(data, TypeDef):
        return data
    try:
        return TypeDef.model_validate(data)
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            loc = [str(p) for p in err["loc"]]
            # ("fields", "0", "key") -> "Field 1 ID", for people rather than code.
            if len(loc) >= 2 and loc[0] == "fields" and loc[1].isdigit():
                rest = [{"key": "ID", "label": "label"}.get(x, x) for x in loc[2:]]
                where = " ".join([f"Field {int(loc[1]) + 1}", *rest])
            else:
                where = " ".join(loc)
            msg = err["msg"].removeprefix(
                "Value error, "
            )  # pydantic's prefix means nothing to people
            problems.append(f"{where}: {msg}" if where else msg)
        raise TypeProblem("Invalid type: " + "; ".join(problems)) from exc


def check_compatible(published: TypeDef, draft: TypeDef) -> None:
    """A published field's kind can't change: stored values would change meaning."""
    before = {f.key: f for f in published.fields}
    for f in draft.fields:
        old = before.get(f.key)
        if old and old.kind != f.kind:
            raise TypeProblem(
                f"Field ID '{f.key}' is a {old.kind} field; its kind can't change once published. "
                "Add a new field instead (and remove this one if it's no longer needed)."
            )
        if old and old.kind == "link" and old.target != f.target:
            raise TypeProblem(
                f"Field '{f.key}' links to {old.target}; that can't change once published."
            )


def describe_changes(published: TypeDef | None, draft: TypeDef) -> list[str]:
    """What publishing ``draft`` changes, in plain words (shown before an admin publishes)."""
    if published is None:
        return [f"New type with {len(draft.fields)} field(s)."]
    out: list[str] = []
    if published.name != draft.name:
        out.append(f"Renames the type from “{published.name}” to “{draft.name}”.")
    if published.description != draft.description:
        out.append("Changes the description.")
    before = {f.key: f for f in published.fields}
    after = {f.key: f for f in draft.fields}
    for f in draft.fields:
        old = before.get(f.key)
        if old is None:
            out.append(f"Adds “{f.label}” ({f.kind}{', required' if f.required else ''}).")
            continue
        if old.label != f.label:
            out.append(f"Relabels “{old.label}” to “{f.label}”.")
        if old.required != f.required:
            out.append(f"Makes “{f.label}” {'required' if f.required else 'optional'}.")
        if old.options != f.options:
            out.append(f"Changes the options of “{f.label}”.")
        if old.help != f.help or old.multiple != f.multiple:
            out.append(f"Changes the settings of “{f.label}”.")
    for key, old in before.items():
        if key not in after:
            out.append(f"Removes “{old.label}” (values already stored are kept, just hidden).")
    if [f.key for f in published.fields if f.key in after] != [
        f.key for f in draft.fields if f.key in before
    ]:
        out.append("Reorders fields.")
    return out or ["No changes."]


def slugify_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "type"
    return key if key[0].isalpha() else f"t_{key}"[:40]


# ---- values ----------------------------------------------------------------------------

CREDENTIAL_RE = re.compile(
    r"^(op://[^\s/]+/[^\s/]+(/[^\s/]+){1,2}"  # op://vault/item[/section]/field
    r"|https://([a-z0-9-]+\.)?1password\.(com|eu|ca)/\S+)$",  # a 1Password item link
    re.IGNORECASE,
)


def _one(f: FieldDef, raw: Any) -> Any:
    if f.kind in ("text", "longtext"):
        s = str(raw).strip()
        s = s if f.kind == "longtext" else " ".join(s.split())
        limit = 20000 if f.kind == "longtext" else 1000
        if len(s) > limit:
            raise TypeProblem(f"{f.label}: at most {limit} characters")
        return s
    if f.kind == "number":
        try:
            n = float(str(raw).replace(",", "").strip())
        except ValueError as exc:
            raise TypeProblem(f"{f.label}: not a number") from exc
        return int(n) if n.is_integer() else n
    if f.kind == "date":
        try:
            return dt.date.fromisoformat(str(raw).strip()[:10]).isoformat()
        except ValueError as exc:
            raise TypeProblem(f"{f.label}: use a date like 2026-12-31") from exc
    if f.kind == "select":
        s = " ".join(str(raw).split())
        match = next((o for o in f.options if o.lower() == s.lower()), None)
        if match is None:
            raise TypeProblem(f"{f.label}: choose one of {', '.join(f.options)}")
        return match
    if f.kind == "url":
        s = str(raw).strip()
        if not re.match(r"^https?://[^\s]+$", s, re.IGNORECASE) or len(s) > 2000:
            raise TypeProblem(f"{f.label}: a web address starting with http:// or https://")
        return s
    if f.kind == "credential":
        s = str(raw).strip()
        if not CREDENTIAL_RE.match(s):
            raise TypeProblem(
                f"{f.label}: paste a 1Password reference (op://vault/item/field) or a 1Password "
                "item link. Never the password or secret itself."
            )
        return s
    # link: {"module": ..., "kind": ..., "id": ...} or "module:kind:id"
    if isinstance(raw, str):
        parts = raw.split(":", 2)
        if len(parts) != 3:
            raise TypeProblem(f"{f.label}: a link looks like module:kind:id")
        raw = {"module": parts[0], "kind": parts[1], "id": parts[2]}
    if not isinstance(raw, dict):
        raise TypeProblem(f"{f.label}: not a link")
    d: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    link = {k: str(d.get(k, "")).strip() for k in ("module", "kind", "id")}
    if not all(link.values()):
        raise TypeProblem(f"{f.label}: a link needs module, kind and id")
    want = (f.target or "").split(".")
    if link["module"] != want[0] or (len(want) > 1 and link["kind"] != want[1]):
        raise TypeProblem(f"{f.label}: must link to {f.target}")
    return link


def _blank(v: Any) -> bool:
    return v is None or v == "" or v == [] or (isinstance(v, str) and not v.strip())


def validate_values(
    definition: TypeDef, values: dict[str, Any], *, require: bool = True
) -> dict[str, Any]:
    """Normalize values for ``definition``. Unknown keys are refused; blanks dropped.
    ``require=False`` for drafts (missing required fields allowed until publish)."""
    known = {f.key: f for f in definition.fields}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise TypeProblem(f"Unknown field(s) for {definition.name}: {', '.join(unknown)}")
    out: dict[str, Any] = {}
    problems: list[str] = []
    for f in definition.fields:
        raw = values.get(f.key)
        items: list[Any] = raw if isinstance(raw, list) else [raw]  # pyright: ignore[reportUnknownVariableType]
        items = [i for i in items if not _blank(i)]
        if not items:
            if require and f.required:
                problems.append(f"{f.label} is required")
            continue
        if len(items) > 1 and not f.multiple:
            problems.append(f"{f.label}: only one value allowed")
            continue
        try:
            cleaned = [_one(f, i) for i in items]
        except TypeProblem as exc:
            problems.append(str(exc))
            continue
        out[f.key] = cleaned if f.multiple else cleaned[0]
    if problems:
        raise TypeProblem("; ".join(problems))
    return out


# ---- storage ---------------------------------------------------------------------------


def draft_permission(module: str) -> str:
    return f"{module}.types.draft"


def approve_permission(module: str) -> str:
    return f"{module}.types.approve"


@dataclass(frozen=True)
class TypeVersion:
    id: int
    version: int
    definition: TypeDef
    note: str
    author: str
    via_ai: bool
    created_at: dt.datetime
    published_at: dt.datetime | None
    published_by: str | None


@dataclass(frozen=True)
class TypeView:
    module: str
    key: str
    retired: bool
    published: TypeVersion | None
    draft: TypeVersion | None  # newer than published, not yet published

    @property
    def status(self) -> str:
        if self.retired:
            return "retired"
        if self.published is None:
            return "draft"
        return "published, with draft changes" if self.draft else "published"

    @property
    def current(self) -> TypeVersion:
        """What people see: the published version, or the draft if never published."""
        v = self.published or self.draft
        assert v is not None
        return v

    def as_dict(self, *, include_draft: bool = False) -> dict[str, Any]:
        shown = (self.draft or self.published) if include_draft else self.current
        assert shown is not None
        return {
            "module": self.module,
            "key": self.key,
            "status": self.status,
            "version": shown.version,
            "showing": "draft" if shown.published_at is None else "published",
            **shown.definition.model_dump(),
        }


_VERSION_COLS = (
    "v.id, v.version, v.definition, v.note, v.author_user, v.author_via_ai, v.created_at,"
    " v.published_at, v.published_by"
)


def _version(row: Any) -> TypeVersion:
    return TypeVersion(
        id=row[0],
        version=row[1],
        definition=TypeDef.model_validate_json(row[2]),
        note=row[3] or "",
        author=row[4],
        via_ai=bool(row[5]),
        created_at=row[6],
        published_at=row[7],
        published_by=row[8],
    )


class TypeStore:
    """Type definitions for every module (core table itops.record_types)."""

    def __init__(self, audit: AuditLog | None = None) -> None:
        self._audit = audit or AuditLog()

    # ---- reads ---------------------------------------------------------------------

    async def get(self, s: AsyncSession, module: str, key: str) -> TypeView | None:
        row = (
            await s.execute(
                text(
                    "SELECT id, current_version_id, retired FROM itops.record_types"
                    " WHERE module = :m AND type_key = :k"
                ),
                {"m": module, "k": key},
            )
        ).first()
        if row is None:
            return None
        versions = await self._versions(s, row[0])
        published = next((v for v in versions if v.id == row[1]), None)
        latest = versions[0] if versions else None
        draft = latest if latest and latest.published_at is None else None
        return TypeView(
            module=module, key=key, retired=bool(row[2]), published=published, draft=draft
        )

    async def list_types(
        self, s: AsyncSession, module: str, *, include_drafts: bool = False
    ) -> list[TypeView]:
        keys = (
            await s.execute(
                text("SELECT type_key FROM itops.record_types WHERE module = :m ORDER BY type_key"),
                {"m": module},
            )
        ).scalars()
        views = [v for k in list(keys) if (v := await self.get(s, module, k)) is not None]
        if not include_drafts:
            views = [v for v in views if v.published is not None and not v.retired]
        return sorted(views, key=lambda v: v.current.definition.name.lower())

    async def history(self, s: AsyncSession, module: str, key: str) -> list[TypeVersion]:
        tid = await self._id(s, module, key)
        return await self._versions(s, tid) if tid is not None else []

    async def definition(self, s: AsyncSession, module: str, key: str) -> TypeDef:
        """The published definition (what records are validated against)."""
        view = await self.get(s, module, key)
        if view is None or view.published is None:
            raise TypeProblem(f"No published type '{key}' in {module}")
        return view.published.definition

    # ---- writes (the caller commits) -------------------------------------------------

    async def draft(
        self,
        s: AsyncSession,
        module: str,
        definition: TypeDef | dict[str, Any],
        *,
        key: str | None = None,
        note: str = "",
    ) -> TypeView:
        """Save a draft: a new type (``key`` None) or changes to an existing one. Replaces
        any earlier unpublished draft of the same type."""
        who = auth.require(draft_permission(module))
        definition = parse_definition(definition)
        note = " ".join(note.split())[:300]
        await self._check_unique_name(s, module, definition.name, key)
        if key is None:
            key = await self._free_key(s, module, slugify_key(definition.name))
            await s.execute(
                text(
                    "INSERT INTO itops.record_types (module, type_key, created_by)"
                    " VALUES (:m, :k, :u)"
                ),
                {"m": module, "k": key, "u": who.user},
            )
        view = await self.get(s, module, key)
        if view is None:
            raise TypeProblem(f"No type '{key}' in {module}")
        if view.retired:
            raise TypeProblem(f"'{key}' is retired. Restore it before changing it.")
        if view.published:
            check_compatible(view.published.definition, definition)
        tid = await self._id(s, module, key)
        params = {
            "d": definition.model_dump_json(),
            "n": note,
            "u": who.user,
            "ai": who.via_ai,
            "c": who.client,
        }
        if view.draft:
            await s.execute(
                text(
                    "UPDATE itops.record_type_versions SET definition = :d, note = :n,"
                    " author_user = :u, author_via_ai = :ai, author_client = :c,"
                    " created_at = sysutcdatetime() WHERE id = :id"
                ),
                {**params, "id": view.draft.id},
            )
            version = view.draft.version
        else:
            version = (view.published.version if view.published else 0) + 1
            await s.execute(
                text(
                    "INSERT INTO itops.record_type_versions (type_id, version, definition, note,"
                    " author_user, author_via_ai, author_client)"
                    " VALUES (:t, :v, :d, :n, :u, :ai, :c)"
                ),
                {**params, "t": tid, "v": version},
            )
        await self._audit.record(
            s, module=module, action="type.drafted", entity_kind="type", entity_id=key,
            title=definition.name,
            summary=f"Drafted version {version} of type “{definition.name}”"
            + (f": {note}" if note else ""),
            details={"version": version, "fields": [f.key for f in definition.fields]},
        )  # fmt: skip
        result = await self.get(s, module, key)
        assert result is not None
        return result

    async def publish(self, s: AsyncSession, module: str, key: str) -> TypeView:
        who = auth.require(approve_permission(module))
        if who.via_ai:  # second lock: the permission is human-only
            raise auth.PermissionDenied("Publishing a type is done by a person.")
        view = await self.get(s, module, key)
        if view is None or view.draft is None:
            raise TypeProblem(f"'{key}' has no draft to publish.")
        await s.execute(
            text(
                "UPDATE itops.record_type_versions SET published_at = sysutcdatetime(),"
                " published_by = :u WHERE id = :id"
            ),
            {"u": who.user, "id": view.draft.id},
        )
        await s.execute(
            text(
                "UPDATE itops.record_types SET current_version_id = :v"
                " WHERE module = :m AND type_key = :k"
            ),
            {"v": view.draft.id, "m": module, "k": key},
        )
        name = view.draft.definition.name
        await self._audit.record(
            s, module=module, action="type.published", entity_kind="type", entity_id=key,
            title=name, summary=f"Published version {view.draft.version} of type “{name}”",
            details={"version": view.draft.version},
        )  # fmt: skip
        result = await self.get(s, module, key)
        assert result is not None
        return result

    async def discard(self, s: AsyncSession, module: str, key: str) -> str:
        """Drop the unpublished draft. Returns 'deleted' (never published) or 'reverted'."""
        auth.require(draft_permission(module))
        view = await self.get(s, module, key)
        if view is None or view.draft is None:
            raise TypeProblem(f"'{key}' has no draft to discard.")
        name = view.draft.definition.name
        tid = await self._id(s, module, key)
        await s.execute(
            text("DELETE FROM itops.record_type_versions WHERE id = :id"), {"id": view.draft.id}
        )
        outcome = "reverted"
        if view.published is None:
            await s.execute(text("DELETE FROM itops.record_types WHERE id = :id"), {"id": tid})
            outcome = "deleted"
        await self._audit.record(
            s, module=module, action="type.discarded", entity_kind="type", entity_id=key,
            title=name, summary=f"Discarded the draft of type “{name}”"
            + (" (never published, so deleted)" if outcome == "deleted" else ""),
        )  # fmt: skip
        return outcome

    async def set_retired(self, s: AsyncSession, module: str, key: str, retired: bool) -> TypeView:
        """Retired types can't be chosen for new records; existing records keep theirs."""
        auth.require(approve_permission(module))
        view = await self.get(s, module, key)
        if view is None or view.published is None:
            raise TypeProblem(f"No published type '{key}' in {module}")
        await s.execute(
            text("UPDATE itops.record_types SET retired = :r WHERE module = :m AND type_key = :k"),
            {"r": retired, "m": module, "k": key},
        )
        name = view.published.definition.name
        await self._audit.record(
            s, module=module, action="type.retired" if retired else "type.restored",
            entity_kind="type", entity_id=key, title=name,
            summary=("Retired" if retired else "Restored") + f" type “{name}”",
        )  # fmt: skip
        result = await self.get(s, module, key)
        assert result is not None
        return result

    async def seed(
        self, s: AsyncSession, module: str, key: str, definition: TypeDef | dict[str, Any]
    ) -> bool:
        """Create and publish a module's starter type as "system" if ``key`` doesn't exist
        yet (ADR 0009). Never touches an existing type, so admins' changes stay."""
        if not KEY_RE.match(key):
            raise TypeProblem(f"{module}: starter type key {key!r} is invalid")
        if await self._id(s, module, key) is not None:
            return False
        definition = parse_definition(definition)
        await s.execute(
            text(
                "INSERT INTO itops.record_types (module, type_key, created_by)"
                " VALUES (:m, :k, 'system')"
            ),
            {"m": module, "k": key},
        )
        tid = await self._id(s, module, key)
        await s.execute(
            text(
                "INSERT INTO itops.record_type_versions (type_id, version, definition, note,"
                " author_user, author_via_ai, author_client, published_at, published_by)"
                " VALUES (:t, 1, :d, 'Starter type', 'system', 0, 'system',"
                " sysutcdatetime(), 'system')"
            ),
            {"t": tid, "d": definition.model_dump_json()},
        )
        vid = (
            await s.execute(
                text("SELECT id FROM itops.record_type_versions WHERE type_id = :t"), {"t": tid}
            )
        ).scalar()
        await s.execute(
            text("UPDATE itops.record_types SET current_version_id = :v WHERE id = :t"),
            {"v": vid, "t": tid},
        )
        await self._audit.record(
            s, module=module, action="type.published", entity_kind="type", entity_id=key,
            title=definition.name, summary=f"Added starter type “{definition.name}”",
            actor=auth.Principal("system", "System", frozenset(), client="system"),
        )  # fmt: skip
        return True

    # ---- helpers ---------------------------------------------------------------------

    async def _check_unique_name(
        self, s: AsyncSession, module: str, name: str, key: str | None
    ) -> None:
        """Two types with the same name would be indistinguishable in pickers."""
        for other in await self.list_types(s, module, include_drafts=True):
            if other.key != key and other.current.definition.name.lower() == name.lower():
                raise TypeProblem(
                    f"There's already a type named “{name}” ({other.key}). Pick another name."
                )

    async def _id(self, s: AsyncSession, module: str, key: str) -> int | None:
        return (
            await s.execute(
                text("SELECT id FROM itops.record_types WHERE module = :m AND type_key = :k"),
                {"m": module, "k": key},
            )
        ).scalar()

    async def _versions(self, s: AsyncSession, type_id: int) -> list[TypeVersion]:
        rows = await s.execute(
            text(
                f"SELECT {_VERSION_COLS} FROM itops.record_type_versions AS v"
                " WHERE v.type_id = :t ORDER BY v.version DESC"
            ),
            {"t": type_id},
        )
        return [_version(r) for r in rows.all()]

    async def _free_key(self, s: AsyncSession, module: str, base: str) -> str:
        key, n = base, 2
        while await self._id(s, module, key) is not None:
            key = f"{base[:36]}_{n}"
            n += 1
        return key
