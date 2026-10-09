"""``itops lint``: the module-boundary and UI-consistency rules, enforced in code.

Runs in pre-commit and CI for every AI tool and human alike. Each violation says how
to fix it. A rule can be bypassed on purpose with a visible exemption on the same line
(or the first line of a file for file-level rules), and the exemption must say why::

    import itops.core.db  # itops: allow import-core — <reason>

It also keeps the exportable code (``core/`` and ``modules/``, AGENTS.md rule 6) free of
company specifics: the patterns to look for are company specifics themselves, so they live
in ``private/export-check.txt`` (one regular expression per line, ``#`` comments), and the
check is skipped where that file doesn't exist.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

# An exemption names the rule and gives a reason after a dash: "— why", "-- why", "- why".
ALLOW_RE = re.compile(r"itops:\s*allow\s+([a-z-]+)\s+(?:—|--|-)\s*\S")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(")
STYLE_ATTR_RE = re.compile(r"\sstyle\s*=", re.IGNORECASE)
STYLE_TAG_RE = re.compile(r"<style\b", re.IGNORECASE)
SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)

HINTS = {
    "import-core": "modules may only import itops.sdk; ask for missing capability in the SDK",
    "import-module": "modules never import each other; use events or module.services.get()",
    "ui-css": "modules don't ship CSS; use core components and tokens (see /_design)",
    "ui-style": "no <style> blocks or style= attributes; use core component classes",
    "ui-color": "no literal colors; colors come from core design tokens",
    "ui-script": "no inline scripts; use htmx/Alpine attributes, or an approved island",
    "theme-tokens": "a theme's theme.css only overrides design tokens (--name: value;)",
    "private-detail": "core/ and modules/ are exported publicly: move company specifics "
    "(names, hosts, domains, addresses) to private/, or use example values",
}
EXPORT_CHECK = "private/export-check.txt"
EXPORTABLE = ("core", "modules")
TEXT_SUFFIXES = {".py", ".sql", ".html", ".jinja", ".j2", ".toml", ".md", ".txt", ".css",
                 ".js", ".json", ".yaml", ".yml", ".ps1", ".cfg", ".ini"}  # fmt: skip
SKIP_PARTS = {"__pycache__", "vendor", "node_modules", ".venv", "dist", "build"}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.detail} ({HINTS[self.rule]})"


def _allowed(line: str, rule: str) -> bool:
    return any(m == rule for m in ALLOW_RE.findall(line))


def _check_python(path: Path, own_package: str, *, is_test: bool) -> list[Violation]:
    source = path.read_text("utf-8")
    lines = source.splitlines()
    out: list[Violation] = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        lineno = node.lineno
        for name in names:
            root = name.split(".")[0]
            line = lines[lineno - 1] if lineno <= len(lines) else ""
            if root == "itops":
                ok = name == "itops.sdk" or name.startswith("itops.sdk.")
                if is_test:
                    ok = ok or name == "itops.testing" or name.startswith("itops.testing.")
                if not ok and not _allowed(line, "import-core"):
                    out.append(Violation(path, lineno, "import-core", f"imports {name}"))
            elif root.startswith("itops_") and root != own_package:
                if not _allowed(line, "import-module"):
                    out.append(Violation(path, lineno, "import-module", f"imports {name}"))
    return out


def _check_text(path: Path, rules: list[tuple[str, re.Pattern[str]]]) -> list[Violation]:
    out: list[Violation] = []
    for i, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        for rule, pattern in rules:
            if pattern.search(line) and not _allowed(line, rule):
                out.append(Violation(path, i, rule, line.strip()[:80]))
    return out


# A theme's stylesheet: comments, @media wrappers, :root blocks and custom properties only.
_THEME_LINE_RE = re.compile(
    r"^\s*(?:$|/\*.*|\*.*|.*\*/\s*$|@media[^{]*\{|:root\s*\{|\}|--[a-zA-Z0-9-]+\s*:.*;"
    r"(?:\s*--[a-zA-Z0-9-]+\s*:.*;)*\s*(?:/\*.*\*/)?\s*$)"
)


def is_theme(module_dir: Path) -> bool:
    """A theme package (core/theme.py) declares the ``itops.themes`` entry point."""
    pyproject = module_dir / "pyproject.toml"
    return pyproject.is_file() and '"itops.themes"' in pyproject.read_text("utf-8")


def lint_theme(theme_dir: Path) -> list[Violation]:
    """Themes override tokens and nothing else: no selectors, no component rules."""
    out: list[Violation] = []
    for css in (theme_dir / "src").rglob("*.css"):
        if css.name != "theme.css":
            out.append(Violation(css, 1, "theme-tokens", "a theme ships only theme.css"))
            continue
        in_comment = False
        for i, line in enumerate(css.read_text("utf-8").splitlines(), start=1):
            stripped = line.strip()
            if in_comment:
                in_comment = "*/" not in stripped
                continue
            if stripped.startswith("/*") and "*/" not in stripped:
                in_comment = True
                continue
            if not _THEME_LINE_RE.match(line) and not _allowed(line, "theme-tokens"):
                out.append(Violation(css, i, "theme-tokens", stripped[:80]))
    return out


def lint_module(module_dir: Path) -> list[Violation]:
    """Lint one module source folder (``modules/<name>`` containing ``src/itops_<name>``)."""
    out: list[Violation] = []
    src = module_dir / "src"
    packages = (
        [p for p in src.iterdir() if p.is_dir() and p.name.startswith("itops_")]
        if src.is_dir()
        else []
    )
    for pkg in packages:
        for py in pkg.rglob("*.py"):
            out += _check_python(py, pkg.name, is_test=False)
        for f in pkg.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix == ".css":
                first = f.read_text("utf-8").splitlines()[:1]
                if not (first and _allowed(first[0], "ui-css")):
                    out.append(Violation(f, 1, "ui-css", "module stylesheet"))
            elif f.suffix in {".html", ".jinja", ".j2"}:
                out += _check_text(
                    f,
                    [
                        ("ui-style", STYLE_TAG_RE),
                        ("ui-style", STYLE_ATTR_RE),
                        ("ui-color", COLOR_RE),
                        ("ui-script", SCRIPT_TAG_RE),
                    ],
                )
    tests = module_dir / "tests"
    if tests.is_dir():
        own = packages[0].name if packages else ""
        for py in tests.rglob("*.py"):
            out += _check_python(py, own, is_test=True)
    return out


def module_dirs(root: Path) -> list[Path]:
    """Every module source folder under ``modules/`` and ``private/``."""
    dirs: list[Path] = []
    for parent in (root / "modules", root / "private"):
        if parent.is_dir():
            dirs += sorted(p for p in parent.iterdir() if (p / "pyproject.toml").is_file())
    return dirs


def export_patterns(root: Path) -> list[re.Pattern[str]]:
    """The company's patterns from ``private/export-check.txt`` (none if it's missing)."""
    path = root / EXPORT_CHECK
    if not path.is_file():
        return []
    lines = [ln.strip() for ln in path.read_text("utf-8").splitlines()]
    return [re.compile(ln) for ln in lines if ln and not ln.startswith("#")]


def lint_exportable(root: Path, patterns: list[re.Pattern[str]] | None = None) -> list[Violation]:
    """Company specifics in the exportable folders (``core/``, ``modules/``)."""
    patterns = export_patterns(root) if patterns is None else patterns
    out: list[Violation] = []
    if not patterns:
        return out
    for folder in EXPORTABLE:
        for f in sorted((root / folder).rglob("*")):
            if not f.is_file() or f.suffix not in TEXT_SUFFIXES or SKIP_PARTS & set(f.parts):
                continue
            try:
                text = f.read_text("utf-8")
            except UnicodeDecodeError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                hit = next((m for p in patterns if (m := p.search(line))), None)
                if hit and not _allowed(line, "private-detail"):
                    out.append(Violation(f, i, "private-detail", f"{hit.group(0)!r}"))
    return out
