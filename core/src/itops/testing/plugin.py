"""pytest plugin (auto-registered via the ``pytest11`` entry point).

Provides the ``database_url`` fixture: the ``itops_test`` database, created if missing.

* Default: the local dev instance with Windows authentication.
* ``ITOPS_TEST_DB_SERVER`` picks another server; ``ITOPS_TEST_DB_USER`` and
  ``ITOPS_TEST_DB_PASSWORD`` switch to SQL authentication (CI's container only).
* ``ITOPS_TEST_DATABASE_URL`` uses a full URL as-is (no auto-create).

Tests that need the database skip when the server isn't reachable.

Tests never read the developer's ``.env`` (it may switch on Entra sign-in or point at
another database): every ``Settings()`` in a test run sees only what the test passes.
"""

from __future__ import annotations

import os
from functools import cache

import pytest
from sqlalchemy import create_engine, text

from itops.core.config import Settings
from itops.core.db import odbc_url, sync_url

TEST_DB = "itops_test"


def pytest_configure(config: pytest.Config) -> None:
    Settings.model_config["env_file"] = None
    # Same for the shell: a developer's ITOPS_* variables mustn't change test behaviour.
    for name in [
        n for n in os.environ if n.startswith("ITOPS_") and not n.startswith("ITOPS_TEST_")
    ]:
        del os.environ[name]


def _url(database: str) -> str:
    return odbc_url(
        os.environ.get("ITOPS_TEST_DB_SERVER", "localhost"),
        database,
        trust_server_certificate=True,
        user=os.environ.get("ITOPS_TEST_DB_USER"),
        password=os.environ.get("ITOPS_TEST_DB_PASSWORD"),
    )


@cache
def _prepare(url: str, *, create: bool) -> str | None:
    """Return None if ready, else the reason the test database is unusable."""
    try:
        if create:
            master = create_engine(sync_url(_url("master")), isolation_level="AUTOCOMMIT")
            with master.connect() as conn:
                conn.execute(text(f"IF DB_ID(N'{TEST_DB}') IS NULL CREATE DATABASE {TEST_DB}"))
            master.dispose()
        eng = create_engine(sync_url(url))
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"


@pytest.fixture(scope="session")
def database_url() -> str:
    explicit = os.environ.get("ITOPS_TEST_DATABASE_URL")
    url = explicit or _url(TEST_DB)
    problem = _prepare(url, create=not explicit)
    if problem:
        message = f"test database not reachable ({problem})"
        # CI sets this so a broken database setup fails loudly instead of skipping.
        if os.environ.get("ITOPS_TEST_REQUIRE_DB"):
            pytest.fail(message)
        pytest.skip(message)
    return url
