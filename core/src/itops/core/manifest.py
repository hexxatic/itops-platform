"""The module manifest (``manifest.toml``): a module's declared contract.

The core trusts the manifest for wiring (dependencies, permissions, events, services)
and verifies that what a module actually registers matches what it declares.
"""

from __future__ import annotations

import re
import tomllib
from importlib import resources

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
# Module names double as SQL Server schema names and URL prefixes, so these are
# off-limits (plus anything starting "db_" or "pg_", checked below).
RESERVED_NAMES = frozenset(
    {
        "itops",
        "core",
        "api",
        "search",  # core routes: /search, /files, /_design
        "files",
        "design",
        "mcp",
        "static",
        "health",
        "tools",  # core: /tools (ADR 0011)
        "connectors",
        "audit",
        "account",
        "dbo",
        "sys",
        "guest",
        "public",
        "information_schema",
    }
)
CORE_REQUIREMENT = "core"
REQ_RE = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)\s*(?P<spec>.*)$")


class ManifestError(ValueError):
    pass


def parse_requirement(req: str) -> tuple[str, SpecifierSet]:
    """``"docs>=0.2"`` -> ``("docs", SpecifierSet(">=0.2"))``."""
    m = REQ_RE.match(req.strip())
    if not m:
        raise ManifestError(f"invalid requirement {req!r} (expected e.g. 'docs>=0.1')")
    try:
        return m["name"], SpecifierSet(m["spec"])
    except InvalidSpecifier as exc:
        raise ManifestError(f"invalid version specifier in {req!r}: {exc}") from exc


class Permission(BaseModel):
    """A declared permission. ``human_only`` ones can never be exercised through an AI
    connection (MCP), whatever the user's role: that is how "AI drafts, humans approve"
    is enforced platform-wide (ADR 0003)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    human_only: bool = False


SCHEDULE_RE = re.compile(r"^(manual|every \d+[mhd]|daily \d{2}:\d{2})$")
OWNS_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
SECRET_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ConnectorDecl(BaseModel):
    """A declared connector (ADR 0010): every flow of data in or out of IT-Ops.

    ``schedule``: ``manual``, ``every 30m`` / ``every 6h`` / ``every 1d``, or ``daily HH:MM``
    (server local time). ``owns``: per record kind (``"assets.device"``), the field keys
    the source system is the source of truth for (``"name"`` for the record's name,
    ``"*"`` for every field); people and AI can't change those. ``secrets``: short names
    read from ``ITOPS_<MODULE>_<NAME>`` in the environment, never from the database.
    ``permissions``: what the connector may do (it acts as itself, not as a person)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    system: str
    direction: str = "in"
    description: str = ""
    schedule: str = "manual"
    permissions: list[str] = Field(default_factory=list[str])
    owns: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    secrets: list[str] = Field(default_factory=list[str])
    missing: str = "flag"  # records gone from the source: "flag" for review, or "ignore"

    @field_validator("direction")
    @classmethod
    def _direction(cls, v: str) -> str:
        if v not in ("in", "out"):
            raise ValueError("direction must be 'in' or 'out'")
        return v

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, v: str) -> str:
        if not SCHEDULE_RE.match(v):
            raise ValueError("schedule must be 'manual', 'every <n>m|h|d' or 'daily HH:MM'")
        return v

    @field_validator("owns")
    @classmethod
    def _owns(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for kind in v:
            if not OWNS_RE.match(kind):
                raise ValueError(f"owns: {kind!r} must be '<module>.<kind>', e.g. 'assets.device'")
        return v

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, v: list[str]) -> list[str]:
        for name in v:
            if not SECRET_RE.match(name):
                raise ValueError(f"secret {name!r}: lowercase letters, digits and underscores")
        return v

    @field_validator("missing")
    @classmethod
    def _missing(cls, v: str) -> str:
        if v not in ("flag", "ignore"):
            raise ValueError("missing must be 'flag' or 'ignore'")
        return v


INPUT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ENDPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*/[A-Za-z][A-Za-z0-9.\-]*$")


class ActionInput(BaseModel):
    """One input of a tool action: a typed, validated value (never a free-form command)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    kind: str = "select"  # select, text, number
    options: list[str] = Field(default_factory=list[str])
    required: bool = True
    help: str = ""
    default: str = ""

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not INPUT_KEY_RE.match(v):
            raise ValueError(f"input key {v!r}: lowercase letters, digits and underscores")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("select", "text", "number"):
            raise ValueError("input kind must be 'select', 'text' or 'number'")
        return v

    @model_validator(mode="after")
    def _options(self) -> ActionInput:
        if self.kind == "select" and not self.options:
            raise ValueError(f"input {self.key!r}: a select needs options")
        return self


class ActionDecl(BaseModel):
    """A declared tool action (ADR 0011): a fixed operation a person runs from /tools.

    ``permission``: one of the module's human-only permissions (AI may only request a
    run). ``danger``: safe, caution or dangerous (the last two ask to confirm).
    ``timeout``: minutes. ``endpoints``: the remote endpoints it uses ("HOST/Endpoint"),
    shown to admins. Every action runs one at a time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    description: str = ""
    permission: str
    inputs: list[ActionInput] = Field(default_factory=list[ActionInput])
    confirm: str = ""
    danger: str = "safe"
    timeout: int = 15
    endpoints: list[str] = Field(default_factory=list[str])
    secrets: list[str] = Field(default_factory=list[str])

    @field_validator("danger")
    @classmethod
    def _danger(cls, v: str) -> str:
        if v not in ("safe", "caution", "dangerous"):
            raise ValueError("danger must be 'safe', 'caution' or 'dangerous'")
        return v

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, v: int) -> int:
        if not 1 <= v <= 240:
            raise ValueError("timeout must be 1-240 minutes")
        return v

    @field_validator("endpoints")
    @classmethod
    def _endpoints(cls, v: list[str]) -> list[str]:
        for e in v:
            if not ENDPOINT_RE.match(e):
                raise ValueError(f"endpoint {e!r} must be 'HOST/EndpointName'")
        return v

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, v: list[str]) -> list[str]:
        for name in v:
            if not SECRET_RE.match(name):
                raise ValueError(f"secret {name!r}: lowercase letters, digits and underscores")
        return v


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    title: str = ""
    description: str = ""
    requires: list[str] = Field(default_factory=list[str])
    optional: list[str] = Field(default_factory=list[str])
    # Service names this module provides via the registry (short names: "search").
    provides: list[str] = Field(default_factory=list[str])
    # Permission name -> description, or a table {description, human_only}.
    # Names are "<module>.<noun>.<verb>" or "<module>.<verb>".
    permissions: dict[str, Permission] = Field(default_factory=dict[str, Permission])
    # Event short name -> description. Published as "<module>.<event>".
    events: dict[str, str] = Field(default_factory=dict[str, str])
    # Connector short name -> declaration (ADR 0010). Run as "<module>.<name>".
    connectors: dict[str, ConnectorDecl] = Field(default_factory=dict[str, ConnectorDecl])
    # Tool action short name -> declaration (ADR 0011). Run as "<module>.<name>".
    actions: dict[str, ActionDecl] = Field(default_factory=dict[str, ActionDecl])

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("must be lowercase letters/digits/underscore, 2-31 chars")
        if v in RESERVED_NAMES or v.startswith(("db_", "pg_")):
            raise ValueError(f"{v!r} is reserved")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        try:
            Version(v)
        except InvalidVersion as exc:
            raise ValueError(f"not a valid version: {v!r}") from exc
        return v

    @field_validator("permissions", mode="before")
    @classmethod
    def _permissions(cls, v: object) -> object:
        # Shorthand: "docs.read" = "View documents"  ==  {description = "View documents"}
        if isinstance(v, dict):
            return {
                k: {"description": d} if isinstance(d, str) else d
                for k, d in v.items()  # pyright: ignore[reportUnknownVariableType]
            }
        return v

    @model_validator(mode="after")
    def _types_permissions(self) -> Manifest:
        """Using record types (ADR 0008) means declaring both type permissions; approving
        is human-only so an AI can draft types but never publish them."""
        draft, approve = f"{self.name}.types.draft", f"{self.name}.types.approve"
        declared = {draft, approve} & set(self.permissions)
        if declared and declared != {draft, approve}:
            raise ValueError(f"declare both {draft} and {approve} to use record types")
        if approve in self.permissions and not self.permissions[approve].human_only:
            raise ValueError(f"{approve} must be human_only (AI drafts types, people publish)")
        if draft in self.permissions and self.permissions[draft].human_only:
            raise ValueError(f"{draft} must not be human_only (AI may draft types)")
        return self

    @model_validator(mode="after")
    def _action_permissions(self) -> Manifest:
        """An action runs under one of the module's own human-only permissions."""
        for name, action in self.actions.items():
            perm = self.permissions.get(action.permission)
            if perm is None:
                raise ValueError(f"action {name!r}: permission {action.permission!r} not declared")
            if not perm.human_only:
                raise ValueError(
                    f"action {name!r}: {action.permission!r} must be human_only "
                    "(AI may request a run, a person starts it)"
                )
        return self

    @property
    def uses_types(self) -> bool:
        return f"{self.name}.types.draft" in self.permissions

    @property
    def human_only_permissions(self) -> frozenset[str]:
        return frozenset(k for k, p in self.permissions.items() if p.human_only)

    @field_validator("requires", "optional")
    @classmethod
    def _reqs(cls, v: list[str]) -> list[str]:
        for req in v:
            parse_requirement(req)
        return v

    def dependency_names(self, *, include_optional: bool = True) -> set[str]:
        names = {parse_requirement(r)[0] for r in self.requires}
        if include_optional:
            names |= {parse_requirement(r)[0] for r in self.optional}
        return names - {CORE_REQUIREMENT}

    @classmethod
    def load(cls, package: str) -> Manifest:
        """Read ``manifest.toml`` bundled inside ``package`` (works from a wheel)."""
        try:
            raw = resources.files(package).joinpath("manifest.toml").read_text("utf-8")
        except FileNotFoundError as exc:
            raise ManifestError(f"{package}: manifest.toml not found in package") from exc
        try:
            return cls.model_validate(tomllib.loads(raw))
        except Exception as exc:  # tomllib / pydantic errors -> one readable type
            raise ManifestError(f"{package}: invalid manifest.toml: {exc}") from exc
