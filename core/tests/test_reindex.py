"""Raising module.search.version re-indexes that module once, at startup (ADR 0015)."""

from __future__ import annotations

import sys
from pathlib import Path

from _helpers import MakeModule, perms
from sqlalchemy import text

from itops.core.app import _reindex_changed, create_app  # pyright: ignore[reportPrivateUsage]
from itops.core.config import Settings

BODY = """
CALLS = []
module.search.version = 2

@module.search.reindexer
async def again() -> int:
    CALLS.append(1)
    return 7
"""


async def test_a_higher_search_version_reindexes_once(
    make_module: MakeModule, database_url: str, tmp_path: Path
) -> None:
    mod = make_module("reidx", manifest_extra=perms("reidx"), body=BODY)
    settings = Settings(database_url_override=database_url, files_dir=str(tmp_path),
                        reindex_on_start=False)  # fmt: skip
    app = create_app(settings, modules=[mod])
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with runtime.sessionmaker() as s:
            await s.execute(text("DELETE FROM itops.search_versions WHERE module = 'reidx'"))
            await s.commit()
        runtime.settings.reindex_on_start = True
        calls: list[int] = sys.modules[mod.package].CALLS  # type: ignore[attr-defined]
        await _reindex_changed(runtime, [mod])
        assert calls == [1]  # never recorded (counts as 1) and now 2: re-indexed
        await _reindex_changed(runtime, [mod])
        assert calls == [1]  # recorded as 2: not again
        async with runtime.sessionmaker() as s:
            sql = "SELECT version FROM itops.search_versions WHERE module = 'reidx'"
            version = (await s.execute(text(sql))).scalar()
        assert version == 2
