"""Module discovery, validation, and dependency ordering.

Modules are found through the ``itops.modules`` entry-point group (installed wheels or
editable workspace members), never by scanning folders. A module that fails validation
or whose required dependencies are missing is refused, along with anything that
requires it; the rest of the platform still starts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import metadata, resources

from packaging.version import Version

from itops import __version__ as CORE_VERSION
from itops.core.manifest import CORE_REQUIREMENT, parse_requirement
from itops.core.migrations import MigrationError, discover
from itops.sdk import Module

ENTRY_POINT_GROUP = "itops.modules"
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
MAX_TOOL_NAME = 64  # common client limit for tool names


@dataclass
class LoadResult:
    loaded: list[Module] = field(default_factory=list[Module])
    failed: dict[str, list[str]] = field(default_factory=dict[str, list[str]])


def validate(module: Module) -> list[str]:
    """Check that what a module registers matches its manifest. Empty list = valid."""
    m = module.manifest
    problems: list[str] = []
    expected_pkg = f"itops_{m.name}"
    if module.package.split(".")[0] != expected_pkg:
        problems.append(f"package must be named {expected_pkg!r}, got {module.package!r}")

    for perm in m.permissions:
        if not perm.startswith(f"{m.name}."):
            problems.append(f"permission {perm!r} must start with '{m.name}.'")

    seen: set[str] = set()
    for tool in module.tools:
        full = f"{m.name}_{tool.name}"
        if not TOOL_NAME_RE.match(tool.name):
            problems.append(f"tool {tool.name!r}: name must be lowercase snake_case")
        if len(full) > MAX_TOOL_NAME:
            problems.append(f"tool {full!r}: name longer than {MAX_TOOL_NAME} chars")
        if tool.name in seen:
            problems.append(f"tool {tool.name!r} registered twice")
        seen.add(tool.name)
        if not tool.description:
            problems.append(f"tool {tool.name!r} has no description (add a docstring)")
        if tool.permission not in m.permissions:
            problems.append(
                f"tool {tool.name!r} uses permission {tool.permission!r}, "
                "which is not declared in manifest.toml [permissions]"
            )
        elif m.permissions[tool.permission].human_only:
            problems.append(
                f"tool {tool.name!r} requires human-only permission {tool.permission!r}; "
                "MCP tools always act via AI, so it could never run. Have the tool create "
                "a draft instead, and keep the human-only step in the web UI."
            )

    templates = resources.files(module.package).joinpath("templates")
    for page in module.pages:
        if not page.path.startswith("/"):
            problems.append(f"page {page.path!r}: path must start with '/'")
        if page.permission and page.permission not in m.permissions:
            problems.append(f"page {page.path!r}: permission {page.permission!r} not declared")
        if not templates.joinpath(page.template).is_file():
            problems.append(f"page {page.path!r}: template templates/{page.template} not found")

    declared, registered = set(m.provides), set(module.service_fns)
    for missing in sorted(declared - registered):
        problems.append(f"manifest provides {missing!r} but no @module.service({missing!r})")
    for extra in sorted(registered - declared):
        problems.append(f"service {extra!r} is registered but not listed in manifest provides")

    declared_c, registered_c = set(m.connectors), {c.name for c in module.connectors}
    for missing in sorted(declared_c - registered_c):
        problems.append(
            f"manifest declares connector {missing!r} but no @module.connector({missing!r})"
        )
    for extra in sorted(registered_c - declared_c):
        problems.append(f"connector {extra!r} is registered but not declared in [connectors]")
    for name in registered_c:
        if not TOOL_NAME_RE.match(name):
            problems.append(f"connector {name!r}: name must be lowercase snake_case")

    declared_a, registered_a = set(m.actions), {a.name for a in module.actions}
    for missing in sorted(declared_a - registered_a):
        problems.append(f"manifest declares action {missing!r} but no @module.action({missing!r})")
    for extra in sorted(registered_a - declared_a):
        problems.append(f"action {extra!r} is registered but not declared in [actions]")
    for name in registered_a:
        if not TOOL_NAME_RE.match(name):
            problems.append(f"action {name!r}: name must be lowercase snake_case")

    for sub in module.subscriptions:
        if not EVENT_RE.match(sub.event):
            problems.append(f"subscription {sub.event!r}: expected '<module>.<event>'")

    for event in m.events:
        if not TOOL_NAME_RE.match(event):
            problems.append(f"event {event!r}: name must be lowercase snake_case")
        if not m.events[event].strip():
            problems.append(f"event {event!r} has no description")

    try:
        discover(module.package, m.name)
    except MigrationError as exc:
        problems.append(str(exc))
    return problems


def discover_installed() -> tuple[list[Module], dict[str, list[str]]]:
    """Load every module registered under the entry-point group."""
    modules: list[Module] = []
    failed: dict[str, list[str]] = {}
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
        except Exception as exc:
            failed[ep.name] = [f"import failed: {exc!r}"]
            continue
        if not isinstance(obj, Module):
            failed[ep.name] = [f"entry point {ep.value!r} is not an itops.sdk.Module"]
            continue
        if obj.name != ep.name:
            failed[ep.name] = [f"entry point name {ep.name!r} != manifest name {obj.name!r}"]
            continue
        modules.append(obj)
    return modules, failed


def resolve(
    modules: list[Module],
    failed: dict[str, list[str]] | None = None,
    *,
    core_version: str = CORE_VERSION,
) -> LoadResult:
    """Validate modules and order them so dependencies load first."""
    result = LoadResult(failed=dict(failed or {}))
    candidates: dict[str, Module] = {}
    for mod in modules:
        if mod.name in candidates:
            result.failed[mod.name] = ["two installed packages claim this module name"]
            continue
        problems = validate(mod)
        for req in mod.manifest.requires:
            name, spec = parse_requirement(req)
            if name == CORE_REQUIREMENT and not spec.contains(
                Version(core_version), prereleases=True
            ):
                problems.append(f"requires core{spec} but core is {core_version}")
        if problems:
            result.failed[mod.name] = problems
        else:
            candidates[mod.name] = mod
    candidates = {n: m for n, m in candidates.items() if n not in result.failed}

    # Drop modules whose required deps are missing/incompatible, until stable.
    changed = True
    while changed:
        changed = False
        for name, mod in list(candidates.items()):
            for req in mod.manifest.requires:
                dep, spec = parse_requirement(req)
                if dep == CORE_REQUIREMENT:
                    continue
                target = candidates.get(dep)
                if target is None:
                    reason = f"required module {dep!r} is not available"
                elif not spec.contains(Version(target.manifest.version), prereleases=True):
                    reason = f"requires {dep}{spec} but {target.manifest.version} is installed"
                else:
                    continue
                result.failed[name] = [reason]
                del candidates[name]
                changed = True
                break

    # Topological order over required + present optional dependencies.
    order: list[Module] = []
    state: dict[str, str] = {}

    def visit(name: str, chain: list[str]) -> bool:
        if state.get(name) == "done":
            return True
        if state.get(name) == "visiting":
            result.failed[name] = [f"dependency cycle: {' -> '.join([*chain, name])}"]
            return False
        state[name] = "visiting"
        mod = candidates[name]
        for dep in sorted(mod.manifest.dependency_names()):
            required = dep in mod.manifest.dependency_names(include_optional=False)
            if not required and state.get(dep) == "visiting":
                # Two modules may use each other optionally (services are looked up when
                # called); only required dependencies can form a cycle.
                continue
            # visit() runs for optional deps too, so they still load first when present.
            if dep in candidates and not visit(dep, [*chain, name]) and required:
                result.failed.setdefault(name, [f"required module {dep!r} failed to load"])
                state[name] = "done"
                return False
        state[name] = "done"
        if name not in result.failed:
            order.append(mod)
        return name not in result.failed

    for name in sorted(candidates):
        visit(name, [])
    result.loaded = order
    return result
