from __future__ import annotations

from pathlib import Path

from itops.core.lint import lint_exportable, lint_module


def make(tmp_path: Path, files: dict[str, str]) -> Path:
    mod = tmp_path / "alpha"
    for rel, content in {"pyproject.toml": "", **files}.items():
        f = mod / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, "utf-8")
    return mod


def rules(mod: Path) -> list[str]:
    return sorted(v.rule for v in lint_module(mod))


def test_clean_module(tmp_path: Path) -> None:
    mod = make(
        tmp_path,
        {
            "src/itops_alpha/__init__.py": "from itops.sdk import Module\nimport os\n",
            "src/itops_alpha/templates/index.html": '{% extends "core/shell.html" %}',
            "tests/test_alpha.py": "from itops.testing import assert_conformant\n",
        },
    )
    assert rules(mod) == []


def test_boundary_violations(tmp_path: Path) -> None:
    mod = make(
        tmp_path,
        {
            "src/itops_alpha/__init__.py": (
                "import itops.core.db\n"
                "from itops_beta import thing\n"
                "from itops.testing import x\n"  # testing is for tests only
            ),
        },
    )
    assert rules(mod) == ["import-core", "import-core", "import-module"]


def test_ui_violations(tmp_path: Path) -> None:
    mod = make(
        tmp_path,
        {
            "src/itops_alpha/templates/a.html": (
                '<div style="margin: 0">x</div>\n'
                "<style>.x{}</style>\n"
                '<span class="c">#ff0000</span>\n'
                "<script>alert(1)</script>\n"
            ),
            "src/itops_alpha/static/extra.css": ".x { }\n",
        },
    )
    assert rules(mod) == ["ui-color", "ui-css", "ui-script", "ui-style", "ui-style"]


def test_visible_exemptions(tmp_path: Path) -> None:
    pkg = "src/itops_alpha"
    mod = make(
        tmp_path,
        {
            f"{pkg}/__init__.py": "import itops.core.db  # itops: allow import-core — why\n",
            f"{pkg}/templates/a.html": "<script src=x></script>{# itops: allow ui-script - x #}\n",
            f"{pkg}/static/island.css": "/* itops: allow ui-css — island */\n",
        },
    )
    assert rules(mod) == []


def test_exemptions_need_a_reason(tmp_path: Path) -> None:
    bare = make(
        tmp_path, {"src/itops_alpha/x.py": "import itops.core.db  # itops: allow import-core\n"}
    )
    assert rules(bare) == ["import-core"]
    (bare / "src/itops_alpha/x.py").write_text(
        "import itops.core.db  # itops: allow import-core - a reason\n", "utf-8"
    )
    assert rules(bare) == []


def test_company_specifics_stay_out_of_exportable_code(tmp_path: Path) -> None:
    """core/ and modules/ are exported: the company's own patterns (kept in private/) are
    refused there, and only there."""
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "export-check.txt").write_text(
        "# acme\n(?i)acme-corp\\.example\n", "utf-8"
    )
    for folder in ("core", "modules/demo", "private/acme"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / folder / "a.py").write_text('HOST = "db.ACME-corp.example"\n', "utf-8")
    found = lint_exportable(tmp_path)
    assert sorted(v.path.parent.name for v in found) == ["core", "demo"]
    assert all(v.rule == "private-detail" for v in found)
    allowed = 'HOST = "db.acme-corp.example"  # itops: allow private-detail - the demo needs it\n'
    (tmp_path / "core" / "a.py").write_text(allowed, "utf-8")
    assert [v.path.parent.name for v in lint_exportable(tmp_path)] == ["demo"]
    (tmp_path / "private" / "export-check.txt").unlink()  # no company file: nothing to check
    assert lint_exportable(tmp_path) == []
