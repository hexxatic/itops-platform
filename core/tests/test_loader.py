from __future__ import annotations

import pytest
from _helpers import TOOL_BODY, MakeModule, perms

from itops.core.loader import resolve, validate
from itops.core.manifest import Manifest


def mod(make_module: MakeModule, name: str, extra: str = ""):
    return make_module(name, manifest_extra=extra + perms(name), body=TOOL_BODY.format(name=name))


def test_valid_module_has_no_problems(make_module: MakeModule) -> None:
    assert validate(mod(make_module, "alpha")) == []


def test_undeclared_permission_is_reported(make_module: MakeModule) -> None:
    m = make_module("alpha", body=TOOL_BODY.format(name="alpha"))
    assert any("not declared" in p for p in validate(m))


def test_tool_without_description_is_reported(make_module: MakeModule) -> None:
    body = '@module.tool(permission="alpha.read")\nasync def ping() -> str:\n    return "x"\n'
    m = make_module("alpha", manifest_extra=perms("alpha"), body=body)
    assert any("no description" in p for p in validate(m))


def test_services_must_match_provides(make_module: MakeModule) -> None:
    body = '@module.service("extra")\ndef extra() -> int:\n    return 1\n'
    m = make_module("alpha", manifest_extra='provides = ["search"]\n', body=body)
    problems = validate(m)
    assert any("provides 'search'" in p for p in problems)
    assert any("'extra' is registered" in p for p in problems)


def test_migrations_must_pair(make_module: MakeModule) -> None:
    m = make_module("alpha", migrations={"0001_init.up.sql": "SELECT 1;"})
    assert any("needs both" in p for p in validate(m))


def test_dependencies_load_first(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'requires = ["beta"]\noptional = ["gamma"]\n')
    b = mod(make_module, "beta")
    g = mod(make_module, "gamma")
    result = resolve([a, b, g])
    names = [m.name for m in result.loaded]
    assert result.failed == {}
    assert names.index("beta") < names.index("alpha")
    assert names.index("gamma") < names.index("alpha")


def test_missing_required_dependency_refuses_module(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'requires = ["beta"]\n')
    result = resolve([a])
    assert [m.name for m in result.loaded] == []
    assert "not available" in result.failed["alpha"][0]


def test_missing_optional_dependency_is_fine(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'optional = ["beta"]\n')
    assert [m.name for m in resolve([a]).loaded] == ["alpha"]


def test_refusal_cascades_to_dependents(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'requires = ["beta"]\n')
    b = make_module("beta", body=TOOL_BODY.format(name="beta"))  # undeclared permission
    result = resolve([a, b])
    assert set(result.failed) == {"alpha", "beta"}


def test_version_constraints(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'requires = ["beta>=2.0"]\n')
    b = mod(make_module, "beta")
    result = resolve([a, b])
    assert "requires beta>=2.0" in result.failed["alpha"][0]


def test_core_version_constraint(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'requires = ["core>=9.0"]\n')
    assert "requires core>=9.0" in resolve([a]).failed["alpha"][0]


def test_cycle_is_refused(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'requires = ["beta"]\n')
    b = mod(make_module, "beta", 'requires = ["alpha"]\n')
    result = resolve([a, b])
    assert result.loaded == []
    assert set(result.failed) == {"alpha", "beta"}


def test_modules_may_use_each_other_optionally(make_module: MakeModule) -> None:
    a = mod(make_module, "alpha", 'optional = ["beta"]\n')
    b = mod(make_module, "beta", 'optional = ["alpha"]\n')
    result = resolve([a, b])
    assert result.failed == {}
    assert {m.name for m in result.loaded} == {"alpha", "beta"}


@pytest.mark.parametrize("bad", ["Docs", "x", "public", "api", "pg_stuff", "with-dash"])
def test_manifest_rejects_bad_names(bad: str) -> None:
    with pytest.raises(ValueError):
        Manifest(name=bad, version="0.1.0")
