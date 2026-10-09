"""Migration parsing and the own-schema rule (no database needed)."""

from __future__ import annotations

import pytest
from _helpers import MakeModule

from itops.core.migrations import batches, schema_problems
from itops.testing import assert_migrations_roundtrip, fulltext_installed


def test_go_splits_batches() -> None:
    sql = "CREATE TABLE docs.a (id int);\nGO\n\ngo -- next\nCREATE VIEW docs.v AS SELECT 1 x;\n"
    assert batches(sql) == ["CREATE TABLE docs.a (id int);", "CREATE VIEW docs.v AS SELECT 1 x;"]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE docs.page (id int)",
        "CREATE TABLE [docs].[page] (id int)",
        "CREATE OR ALTER VIEW docs.v AS SELECT 1 x",
        "DROP TABLE IF EXISTS docs.page",
        "CREATE UNIQUE NONCLUSTERED INDEX ix_page ON docs.page (id)",
        "ALTER TABLE docs.page ADD CONSTRAINT fk FOREIGN KEY (x) REFERENCES docs.book (id)",
        "CREATE FULLTEXT CATALOG docs_catalog",
        "CREATE FULLTEXT INDEX ON docs.page (body) KEY INDEX pk_page ON docs_catalog",
        "-- CREATE TABLE helpdesk.x (id int)\nSELECT 'CREATE TABLE helpdesk.y' AS s",
    ],
)
def test_own_schema_is_allowed(sql: str) -> None:
    assert schema_problems("docs", sql, "0001_x.up.sql") == []


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE page (id int)",
        "CREATE TABLE dbo.page (id int)",
        "CREATE TABLE helpdesk.ticket (id int)",
        "CREATE INDEX ix ON helpdesk.ticket (id)",
        "ALTER TABLE docs.page ADD FOREIGN KEY (t) REFERENCES helpdesk.ticket (id)",
        "CREATE FULLTEXT CATALOG shared_catalog",
    ],
)
def test_other_schemas_are_refused(sql: str) -> None:
    assert schema_problems("docs", sql, "0001_x.up.sql")


FULLTEXT_UP = """
CREATE TABLE ftmod.page (
    id int NOT NULL CONSTRAINT pk_page PRIMARY KEY,
    body nvarchar(max) NOT NULL
);
GO
CREATE FULLTEXT CATALOG ftmod_catalog;
GO
CREATE FULLTEXT INDEX ON ftmod.page (body) KEY INDEX pk_page ON ftmod_catalog;
"""
FULLTEXT_DOWN = """
DROP FULLTEXT INDEX ON ftmod.page;
GO
DROP FULLTEXT CATALOG ftmod_catalog;
GO
DROP TABLE ftmod.page;
"""


async def test_fulltext_migration_roundtrip(make_module: MakeModule, database_url: str) -> None:
    """Full-text DDL can't run in a transaction; the runner must handle it."""
    if not fulltext_installed(database_url):
        pytest.skip("Full-Text Search is not installed on the test server")
    m = make_module(
        "ftmod",
        migrations={"0001_pages.up.sql": FULLTEXT_UP, "0001_pages.down.sql": FULLTEXT_DOWN},
    )
    await assert_migrations_roundtrip(m, database_url)


async def test_core_migrations_roundtrip(database_url: str) -> None:
    """The core's own tables (schema itops) must also go up, down and up cleanly."""
    from itops.core import migrations
    from itops.core.db import make_engine

    migs = migrations.discover("itops", "itops")
    assert [m.name for m in migs][:1] == ["files"]
    engine = make_engine(database_url)
    try:
        await migrations.upgrade_core(engine)
        down = await migrations.downgrade(engine, "itops", migs, to=0)
        assert down == sorted((m.version for m in migs), reverse=True)
        assert await migrations.upgrade_core(engine) == [m.version for m in migs]
    finally:
        await engine.dispose()
