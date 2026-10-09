"""Module conformance suite. Every module's tests call these; CI runs them all.

    from itops.testing import assert_conformant, assert_migrations_roundtrip
    from itops_docs import module

    def test_conformance():
        assert_conformant(module)

    async def test_migrations(database_url):
        await assert_migrations_roundtrip(module, database_url)

``database_url`` is a pytest fixture provided by the ``itops`` pytest plugin; it skips
when no test database is reachable.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from itops.core import migrations
from itops.core.db import make_engine, sync_url
from itops.core.lint import lint_module
from itops.core.loader import validate
from itops.sdk import Module

__all__ = [
    "assert_conformant",
    "assert_migrations_roundtrip",
    "conformance_problems",
    "fulltext_installed",
]


def _source_dir(module: Module) -> Path | None:
    """``modules/<name>`` when running from a source checkout, else ``None`` (wheel)."""
    import importlib

    pkg = importlib.import_module(module.package)
    if not pkg.__file__:
        return None
    pkg_dir = Path(pkg.__file__).resolve().parent
    candidate = pkg_dir.parent.parent  # <module_dir>/src/itops_<name>
    return candidate if (candidate / "pyproject.toml").is_file() else None


def conformance_problems(module: Module) -> list[str]:
    problems = validate(module)
    if not (
        module.tools or module.pages or module.subscriptions or module.connectors or module.actions
    ):
        problems.append(
            "module registers nothing (no tools, pages, subscriptions, connectors or actions)"
        )
    src = _source_dir(module)
    if src is not None:
        problems += [str(v) for v in lint_module(src)]
        if not any((src / "tests").glob("test_*.py")):
            problems.append("module has no tests (tests/test_*.py)")
        for doc in ("AGENTS.md", "CLAUDE.md"):
            if not (src / doc).is_file():
                problems.append(f"module is missing {doc}")
    return problems


def assert_conformant(module: Module) -> None:
    problems = conformance_problems(module)
    assert not problems, f"{module.name} is not conformant:\n  - " + "\n  - ".join(problems)


async def assert_migrations_roundtrip(module: Module, database_url: str) -> None:
    """Up, fully down, and up again must all succeed cleanly."""
    migs = migrations.discover(module.package, module.name)
    engine = make_engine(database_url)
    try:
        await migrations.downgrade(engine, module.name, migs, to=0)
        up = await migrations.upgrade(engine, module.name, migs)
        assert up == [m.version for m in migs], f"upgrade applied {up}"
        down = await migrations.downgrade(engine, module.name, migs, to=0)
        assert down == sorted(up, reverse=True), f"downgrade reverted {down}"
        again = await migrations.upgrade(engine, module.name, migs)
        assert again == up, "re-upgrade after downgrade did not match"
    finally:
        await engine.dispose()


def fulltext_installed(database_url: str) -> bool:
    """Whether the server has Full-Text Search (the Linux CI container doesn't)."""
    eng = create_engine(sync_url(database_url))
    try:
        query = text("SELECT FULLTEXTSERVICEPROPERTY('IsFullTextInstalled')")
        with eng.connect() as conn:
            return bool(conn.execute(query).scalar())
    finally:
        eng.dispose()
