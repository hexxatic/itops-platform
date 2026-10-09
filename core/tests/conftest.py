"""Fixtures that build real (throwaway) module packages on disk, so the loader and
validator are tested against the same things a wheel would provide."""

from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from _helpers import MakeModule

from itops.sdk import Module


@pytest.fixture
def make_module(tmp_path: Path) -> Iterator[MakeModule]:
    """``make_module("alpha", manifest_extra='requires = ["beta"]', body="...")``."""
    root = tmp_path / "pkgs"
    root.mkdir()
    sys.path.insert(0, str(root))
    created: list[str] = []

    def factory(
        name: str,
        *,
        manifest_extra: str = "",
        body: str = "",
        version: str = "0.1.0",
        migrations: dict[str, str] | None = None,
        templates: dict[str, str] | None = None,
    ) -> Module:
        pkg = f"itops_{name}"
        d = root / pkg
        d.mkdir()
        manifest = f'name = "{name}"\nversion = "{version}"\n' + textwrap.dedent(manifest_extra)
        (d / "manifest.toml").write_text(manifest, "utf-8")
        for folder, files in (("migrations", migrations), ("templates", templates)):
            if files:
                (d / folder).mkdir()
                for fname, content in files.items():
                    (d / folder / fname).write_text(content, "utf-8")
        code = "from itops.sdk import Module\nmodule = Module(__name__)\n" + textwrap.dedent(body)
        (d / "__init__.py").write_text(code, "utf-8")
        created.append(pkg)
        return importlib.import_module(pkg).module

    yield factory
    sys.path.remove(str(root))
    for pkg in created:
        sys.modules.pop(pkg, None)
