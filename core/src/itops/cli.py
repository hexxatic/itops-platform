"""The ``itops`` command. Plain CLI so every AI tool and human uses the same paved road.

itops serve [--reload]              run the platform
itops new module <name> [--title]   scaffold a conformant module
itops modules list|check            show / validate installed modules
itops modules sync                  install the wheels in the modules folder
itops db upgrade|status             apply / show migrations
itops db downgrade <module> [--to]  revert a module's migrations
itops lint [module_dir ...]         module boundary + UI consistency rules
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template

APP_FACTORY = "itops.core.app:create_app"


def _repo_root() -> Path:
    here = Path.cwd().resolve()
    for p in (here, *here.parents):
        if (p / "AGENTS.md").is_file() and (p / "core").is_dir():
            return p
    return here


# ---- serve --------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        APP_FACTORY,
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        # MCP clients (Claude, ChatGPT) hold a stream open indefinitely; without a limit,
        # Ctrl+C / a service stop waits on it forever. Give open requests 5 s, then close.
        timeout_graceful_shutdown=5,
        # Keep the real peer address: behind IIS it's loopback, which is how the sign-in gate
        # knows X-Forwarded-Host came from the local proxy (entra._public_host). uvicorn's
        # own handling would swap in the browser's address first and hide that.
        proxy_headers=False,
    )
    return 0


# ---- new module ---------------------------------------------------------------------


def cmd_new_module(args: argparse.Namespace) -> int:
    from itops.core.manifest import Manifest

    name: str = args.name
    try:
        Manifest(name=name, version="0.1.0")
    except Exception as exc:
        print(f"invalid module name {name!r}: {exc}", file=sys.stderr)
        return 2
    title: str = args.title or name.replace("_", " ").title()
    pkg = f"itops_{name}"
    root = _repo_root()
    dest = root / ("private" if args.private else "modules") / name
    if dest.exists():
        print(f"{dest} already exists", file=sys.stderr)
        return 2

    values = {"name": name, "title": title, "pkg": pkg}
    template_root = resources.files("itops").joinpath("_scaffold", "module")

    def copy(node: Traversable, rel: Path) -> None:
        for child in node.iterdir():
            target_name = child.name.replace("__pkg__", pkg).replace("__name__", name)
            if child.is_dir():
                copy(child, rel / target_name)
            elif target_name.endswith(".tmpl"):
                out = dest / rel / target_name.removesuffix(".tmpl")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(Template(child.read_text("utf-8")).substitute(values), "utf-8")

    copy(template_root, Path())
    print(f"created {dest.relative_to(root)}")
    print("next: uv sync --all-packages   (installs it editable, then it loads on restart)")
    return 0


# ---- modules ------------------------------------------------------------------------


def cmd_modules(args: argparse.Namespace) -> int:
    from itops.core import loader

    if args.action == "sync":
        return _sync_wheels(Path(args.dir or _settings().modules_dir))

    found, failed = loader.discover_installed()
    result = loader.resolve(found, failed)
    for mod in result.loaded:
        m = mod.manifest
        print(f"  ok       {m.name:<16} {m.version:<10} {m.title}")
    for name, problems in result.failed.items():
        print(f"  REFUSED  {name}")
        for p in problems:
            print(f"             - {p}")
    if not result.loaded and not result.failed:
        print("  no modules installed")
    return 1 if (args.action == "check" and result.failed) else 0


def _sync_wheels(folder: Path) -> int:
    wheels = sorted(folder.glob("*.whl"))
    if not wheels:
        print(f"no wheels in {folder}")
        return 0
    uv = shutil.which("uv")
    base = (
        [uv, "pip", "install", "--python", sys.executable]
        if uv
        else [sys.executable, "-m", "pip", "install"]
    )
    return subprocess.call([*base, *map(str, wheels)])


# ---- db -----------------------------------------------------------------------------


def _settings():
    from itops.core.config import get_settings

    return get_settings()


def cmd_db(args: argparse.Namespace) -> int:
    from itops.core import loader, migrations
    from itops.core.db import make_engine

    found, failed = loader.discover_installed()
    mods = {m.name: m for m in loader.resolve(found, failed).loaded}

    async def run() -> int:
        engine = make_engine(_settings().database_url)
        try:
            if args.action == "upgrade":
                core = await migrations.upgrade_core(engine)
                print(f"  {'(core)':<16} applied {core or 'nothing'}")
                for mod in mods.values():
                    done = await migrations.upgrade(
                        engine, mod.name, migrations.discover(mod.package, mod.name)
                    )
                    print(f"  {mod.name:<16} applied {done or 'nothing'}")
            elif args.action == "status":
                await migrations.ensure_core(engine)
                async with engine.connect() as conn:
                    have = await migrations.applied_versions(conn, "itops")
                    total = len(migrations.discover("itops", "itops"))
                    print(f"  {'(core)':<16} {len(have)}/{total} applied")
                    for mod in mods.values():
                        have = await migrations.applied_versions(conn, mod.name)
                        total = len(migrations.discover(mod.package, mod.name))
                        print(f"  {mod.name:<16} {len(have)}/{total} applied")
            elif args.action == "downgrade":
                mod = mods.get(args.module)
                if mod is None:
                    print(f"unknown module {args.module!r}", file=sys.stderr)
                    return 2
                done = await migrations.downgrade(
                    engine, mod.name, migrations.discover(mod.package, mod.name), to=args.to
                )
                print(f"  {mod.name:<16} reverted {done or 'nothing'}")
        finally:
            await engine.dispose()
        return 0

    return asyncio.run(run())


# ---- lint ---------------------------------------------------------------------------


def cmd_lint(args: argparse.Namespace) -> int:
    from itops.core.lint import is_theme, lint_exportable, lint_module, lint_theme, module_dirs

    dirs = [Path(d) for d in args.paths] if args.paths else module_dirs(_repo_root())
    violations = [v for d in dirs for v in (lint_theme(d) if is_theme(d) else lint_module(d))]
    if not args.paths:  # the whole repository: also keep core/ and modules/ exportable
        violations += lint_exportable(_repo_root())
    for v in violations:
        print(v)
    if violations:
        print(
            f"\n{len(violations)} violation(s). Fix them, or add a visible exemption: "
            "# itops: allow <rule> — <reason>"
        )
        return 1
    print(f"lint ok ({len(dirs)} module(s))")
    return 0


# ---- search -------------------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    """Rebuild the search index from every module's published content."""
    from itops.core.app import create_app

    async def run() -> int:
        app = create_app()
        async with app.router.lifespan_context(app):
            for mod in app.state.load_result.loaded:
                for reindex in mod.search.reindexers:
                    count = await reindex()
                    print(f"  {mod.name:<16} indexed {count}")
        return 0

    return asyncio.run(run())


# ---- entry --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="itops", description="IT-Ops platform CLI")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the platform")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8800)
    s.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    s.set_defaults(func=cmd_serve)

    n = sub.add_parser("new", help="scaffold something new")
    nsub = n.add_subparsers(dest="kind", required=True)
    nm = nsub.add_parser("module", help="scaffold a conformant module")
    nm.add_argument("name")
    nm.add_argument("--title")
    nm.add_argument(
        "--private",
        action="store_true",
        help="create under private/ (company-only, never exported)",
    )
    nm.set_defaults(func=cmd_new_module)

    m = sub.add_parser("modules", help="installed modules")
    m.add_argument("action", choices=["list", "check", "sync"])
    m.add_argument("--dir", help="wheel folder for sync (default: ITOPS_MODULES_DIR)")
    m.set_defaults(func=cmd_modules)

    d = sub.add_parser("db", help="database migrations")
    d.add_argument("action", choices=["upgrade", "downgrade", "status"])
    d.add_argument("module", nargs="?")
    d.add_argument("--to", type=int, default=0)
    d.set_defaults(func=cmd_db)

    li = sub.add_parser("lint", help="module boundary + UI consistency rules")
    li.add_argument("paths", nargs="*")
    li.set_defaults(func=cmd_lint)

    sr = sub.add_parser("search", help="search index maintenance")
    sr.add_argument("action", choices=["rebuild"])
    sr.set_defaults(func=cmd_search)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
