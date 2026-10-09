"""Database engine. One SQL Server database; every module owns one schema.

Connections go through Microsoft ODBC Driver 18 (``pyodbc``, async via ``aioodbc``).
Production and dev use Windows authentication, so no password lives in config: the
service connects as its gMSA, a developer as themselves.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

CORE_SCHEMA = "itops"
ASYNC_DIALECT = "mssql+aioodbc"
SYNC_DIALECT = "mssql+pyodbc"


def _odbc_value(value: str) -> str:
    """Brace-quote an ODBC attribute value when it contains special characters."""
    if any(c in value for c in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def odbc_url(
    server: str,
    database: str,
    *,
    driver: str = "ODBC Driver 18 for SQL Server",
    trust_server_certificate: bool = False,
    user: str | None = None,
    password: str | None = None,
    dialect: str = ASYNC_DIALECT,
) -> str:
    """SQLAlchemy URL for SQL Server. Windows auth unless ``user`` is given (CI only).

    ``server`` may be a named instance (``SQLHOST\\INSTANCE``) or ``host,port``; it
    travels inside ``odbc_connect`` so the backslash needs no URL escaping.
    """
    attrs = {
        "DRIVER": "{" + driver + "}",
        "SERVER": _odbc_value(server),
        "DATABASE": _odbc_value(database),
        "Encrypt": "yes",
        "TrustServerCertificate": "yes" if trust_server_certificate else "no",
    }
    if user is None:
        attrs["Trusted_Connection"] = "yes"
    else:
        attrs["UID"] = _odbc_value(user)
        attrs["PWD"] = _odbc_value(password or "")
    conn = ";".join(f"{k}={v}" for k, v in attrs.items())
    return f"{dialect}:///?odbc_connect={quote_plus(conn)}"


def sync_url(url: str) -> str:
    """The same URL for the synchronous pyodbc dialect (admin tasks, reachability)."""
    return url.replace(ASYNC_DIALECT, SYNC_DIALECT, 1)


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def quote_ident(name: str) -> str:
    """Bracket-quote an identifier. Module names are already validated to ``[a-z0-9_]``."""
    return "[" + name.replace("]", "]]") + "]"
