"""Per-module T-SQL migrations (ADR 0001, updated by ADR 0002).

A module ships ``migrations/NNNN_<name>.up.sql`` and a matching ``.down.sql``:

* Plain T-SQL. Every object is **schema-qualified with the module's own schema**
  (``docs.page``); SQL Server has no ``search_path``. :func:`schema_problems` enforces
  this, including foreign keys, so a module can't reach into another's tables.
* ``GO`` on its own line separates batches, as in SSMS/sqlcmd.
* Each migration runs in one transaction, except ones that touch full-text (SQL
  Server refuses full-text DDL inside a transaction), which run in autocommit.

Applied versions are tracked in ``itops.schema_migrations``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from itops.core.db import CORE_SCHEMA, quote_ident

FILE_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.(?P<dir>up|down)\.sql$")
GO_RE = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)
FULLTEXT_RE = re.compile(r"\bFULLTEXT\b", re.IGNORECASE)
_COMMENTS_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRINGS_RE = re.compile(r"N?'(?:[^']|'')*'")
_NAME = r"(?P<obj>(?:\[[^\]]+\]|\w+)(?:\s*\.\s*(?:\[[^\]]+\]|\w+))*)"
_QUALIFIED_RULES = [
    re.compile(
        r"\b(?:CREATE(?:\s+OR\s+ALTER)?|ALTER|DROP)\s+"
        r"(?:TABLE|VIEW|PROC|PROCEDURE|FUNCTION|TRIGGER|SEQUENCE|TYPE|SYNONYM)\s+"
        r"(?:IF\s+EXISTS\s+)?" + _NAME,
        re.IGNORECASE,
    ),
    # "KEY INDEX pk ON <catalog>" (full-text) names a catalog, not a table: skip it.
    re.compile(r"(?<!\bKEY )\bINDEX\s+(?:\[[^\]]+\]|\w+)\s+ON\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\bFULLTEXT\s+INDEX\s+ON\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\bREFERENCES\s+" + _NAME, re.IGNORECASE),
]
_CATALOG_RE = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+FULLTEXT\s+CATALOG\s+(?P<obj>\[[^\]]+\]|\w+)", re.IGNORECASE
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str


def batches(sql: str) -> list[str]:
    """Split a script on ``GO`` lines, dropping empty batches."""
    return [b.strip() for b in GO_RE.split(sql) if b.strip()]


def _unquote(name: str) -> str:
    return name.strip().strip("[]").lower()


def schema_problems(module: str, sql: str, filename: str) -> list[str]:
    """Objects a migration touches must live in the module's own schema."""
    clean = _STRINGS_RE.sub("''", _COMMENTS_RE.sub(" ", sql))
    clean = re.sub(r"\s+", " ", clean)  # one space everywhere, so "KEY INDEX" matches
    problems: list[str] = []
    for rule in _QUALIFIED_RULES:
        for m in rule.finditer(clean):
            parts = [p for p in re.split(r"\s*\.\s*", m["obj"]) if p]
            if len(parts) < 2 or _unquote(parts[-2]) != module:
                problems.append(
                    f"{filename}: {m['obj']!r} must be qualified with the module's schema "
                    f"('{module}.<name>'); modules only touch their own schema"
                )
    for m in _CATALOG_RE.finditer(clean):
        if not _unquote(m["obj"]).startswith(f"{module}_"):
            problems.append(
                f"{filename}: full-text catalog {m['obj']!r} must start with '{module}_'"
            )
    return problems


def discover(package: str, module: str | None = None) -> list[Migration]:
    """Load a package's bundled migrations, validating naming, pairing and schemas."""
    folder = resources.files(package).joinpath("migrations")
    if not folder.is_dir():
        return []
    found: dict[int, dict[str, str]] = {}
    names: dict[int, str] = {}
    problems: list[str] = []
    for entry in folder.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        m = FILE_RE.match(entry.name)
        if not m:
            raise MigrationError(
                f"{package}: bad migration filename {entry.name!r} "
                "(expected NNNN_name.up.sql / NNNN_name.down.sql)"
            )
        version = int(m["version"])
        if names.setdefault(version, m["name"]) != m["name"]:
            raise MigrationError(f"{package}: migration {version:04d} has two different names")
        sql = entry.read_text("utf-8")
        if module:
            problems += schema_problems(module, sql, entry.name)
        found.setdefault(version, {})[m["dir"]] = sql
    if problems:
        raise MigrationError(f"{package}: " + "; ".join(problems))
    out: list[Migration] = []
    for i, version in enumerate(sorted(found), start=1):
        if version != i:
            raise MigrationError(
                f"{package}: migrations must be numbered 0001, 0002, ... (gap at {i:04d})"
            )
        pair = found[version]
        if "up" not in pair or "down" not in pair:
            raise MigrationError(
                f"{package}: migration {version:04d} needs both .up.sql and .down.sql"
            )
        out.append(Migration(version, names[version], pair["up"], pair["down"]))
    return out


async def _ensure_schema(conn: AsyncConnection, schema: str) -> None:
    await conn.execute(
        text(f"IF SCHEMA_ID(:s) IS NULL EXEC(N'CREATE SCHEMA {quote_ident(schema)}')"),
        {"s": schema},
    )


async def ensure_core(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await _ensure_schema(conn, CORE_SCHEMA)
        await conn.execute(
            text(
                f"IF OBJECT_ID(N'{CORE_SCHEMA}.schema_migrations', N'U') IS NULL "
                f"CREATE TABLE {CORE_SCHEMA}.schema_migrations ("
                " module nvarchar(64) NOT NULL,"
                " version int NOT NULL,"
                " name nvarchar(200) NOT NULL,"
                " applied_at datetime2 NOT NULL DEFAULT sysutcdatetime(),"
                " CONSTRAINT pk_schema_migrations PRIMARY KEY (module, version))"
            )
        )


async def applied_versions(conn: AsyncConnection, module: str) -> list[int]:
    rows = await conn.execute(
        text(
            f"SELECT version FROM {CORE_SCHEMA}.schema_migrations"
            " WHERE module = :m ORDER BY version"
        ),
        {"m": module},
    )
    return [int(r[0]) for r in rows]


async def _run(engine: AsyncEngine, sql: str, record: str, params: dict[str, object]) -> None:
    """Run a migration script, then its bookkeeping statement."""
    if FULLTEXT_RE.search(sql):
        # Full-text DDL can't run inside a user transaction.
        async with engine.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            for batch in batches(sql):
                await conn.exec_driver_sql(batch)
            await conn.execute(text(record), params)
        return
    async with engine.begin() as conn:
        for batch in batches(sql):
            await conn.exec_driver_sql(batch)
        await conn.execute(text(record), params)


async def upgrade_core(engine: AsyncEngine) -> list[int]:
    """The core's own tables (schema ``itops``, e.g. files). Always runs before modules."""
    return await upgrade(engine, CORE_SCHEMA, discover("itops", CORE_SCHEMA))


async def upgrade(engine: AsyncEngine, module: str, migrations: list[Migration]) -> list[int]:
    """Apply pending migrations; returns the versions applied."""
    await ensure_core(engine)
    async with engine.begin() as conn:
        await _ensure_schema(conn, module)
        done = set(await applied_versions(conn, module))
    applied: list[int] = []
    for mig in migrations:
        if mig.version in done:
            continue
        await _run(
            engine,
            mig.up,
            f"INSERT INTO {CORE_SCHEMA}.schema_migrations (module, version, name)"
            " VALUES (:m, :v, :n)",
            {"m": module, "v": mig.version, "n": mig.name},
        )
        applied.append(mig.version)
    return applied


async def downgrade(
    engine: AsyncEngine, module: str, migrations: list[Migration], *, to: int = 0
) -> list[int]:
    """Revert applied migrations above version ``to``; returns the versions reverted."""
    await ensure_core(engine)
    by_version = {m.version: m for m in migrations}
    async with engine.connect() as conn:
        done = await applied_versions(conn, module)
    reverted: list[int] = []
    for version in sorted((v for v in done if v > to), reverse=True):
        mig = by_version.get(version)
        if mig is None:
            raise MigrationError(
                f"{module}: applied migration {version:04d} is missing from the package"
            )
        await _run(
            engine,
            mig.down,
            f"DELETE FROM {CORE_SCHEMA}.schema_migrations WHERE module = :m AND version = :v",
            {"m": module, "v": version},
        )
        reverted.append(version)
    return reverted
