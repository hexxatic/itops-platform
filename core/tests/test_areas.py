"""Visibility of areas by AD group (ADR 0012): who a restriction hides an area from."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from itops.core.areas import (  # pyright: ignore[reportPrivateUsage]
    AreaError,
    Areas,
    Restriction,
    _describe,
    clean_groups,
)
from itops.core.auth import ALL, Principal
from itops.core.identity import Identity, group_names, principal_from_access_token
from itops.core.manifest import Manifest
from itops.sdk import Search

READER = Principal("pat@example.com", "Pat", frozenset({"docs.read"}))
MEMBER = replace(READER, groups=frozenset({"app-itops-security"}))
ADMIN = Principal("admin@example.com", "Admin", frozenset({ALL}))
CONNECTOR = Principal("connector:x.y", "Import", frozenset({"docs.write"}), client="connector")


def ai(p: Principal) -> Principal:
    return replace(p, via_ai=True, client="mcp")


def test_a_group_restriction_hides_the_area_from_everyone_else() -> None:
    r = Restriction("docs", "collection", "7", ("App-ITOps-Security",), no_ai=False)
    assert r.hides_from(READER) and r.hides_from(ai(READER))
    assert not r.hides_from(MEMBER) and not r.hides_from(ai(MEMBER))  # names ignore case
    assert not r.hides_from(ADMIN) and not r.hides_from(ai(ADMIN))
    assert not r.hides_from(CONNECTOR) and not r.hides_from(None)  # not people


def test_kept_out_of_ai_hides_it_from_every_ai_connection_only() -> None:
    r = Restriction("docs", "collection", "7", (), no_ai=True)
    assert r.hides_from(ai(ADMIN)) and r.hides_from(ai(MEMBER))
    assert not r.hides_from(ADMIN) and not r.hides_from(READER)
    both = replace(r, groups=("App-ITOps-Security",))
    assert both.hides_from(ai(MEMBER)) and not both.hides_from(MEMBER)


def test_group_names_are_cleaned_as_admins_type_them() -> None:
    assert clean_groups(["CORP\\App-ITOps-Security, app-itops-security\nVendors "]) == [
        "App-ITOps-Security",
        "Vendors",
    ]
    with pytest.raises(AreaError, match="isn't an AD group name"):
        clean_groups(["bad*name"])
    with pytest.raises(AreaError, match="At most"):
        clean_groups([",".join(f"G{i}" for i in range(21))])
    assert group_names(["CORP\\App-ITOps-Editors", "Domain Users", " "]) == (
        "app-itops-editors",
        "domain users",
    )


def test_changes_are_described_for_the_audit_trail() -> None:
    before = Restriction("docs", "collection", "7", ("A",), no_ai=False)
    assert _describe(None, ["A"], False) == ("area.restricted", "Visible only to A (and admins)")
    assert _describe(before, ["a"], False) == ("", "")  # no change
    assert _describe(before, ["A"], True) == (
        "area.changed",
        "Visible only to A (and admins); kept out of AI connections",
    )
    assert _describe(None, [], True) == ("area.restricted", "Kept out of AI connections")
    assert _describe(before, [], False)[0] == "area.opened"


def test_a_module_with_areas_names_them() -> None:
    base = {"name": "notes", "version": "0.1.0", "areas": {"folder": "Folder"}}
    with pytest.raises(ValueError, match="describe_area"):
        Manifest.model_validate(base)
    with pytest.raises(ValueError, match="area_entities"):
        Manifest.model_validate({**base, "provides": ["describe_area"]})
    both = ["describe_area", "area_entities"]
    assert Manifest.model_validate({**base, "provides": both}).areas == {"folder": "Folder"}
    with pytest.raises(ValueError, match="area kind"):
        Manifest.model_validate({**base, "areas": {"Bad-Kind": "x"}, "provides": both})


def test_a_search_entry_may_carry_an_area_another_module_handed_over() -> None:
    """kb indexes Docs' how-tos with their Docs audience (ADR 0028)."""
    areas = Areas()
    areas.declare("docs", {"audience": "Help-site audience"})
    module = SimpleNamespace(
        name="kb", runtime=SimpleNamespace(areas=areas),
        areas=SimpleNamespace(id=lambda kind, key: f"kb:{kind}:{key}"),
    )  # fmt: skip
    search = Search(module)  # type: ignore[arg-type]
    assert search._area("docs:audience:5") == "docs:audience:5"  # pyright: ignore[reportPrivateUsage]
    assert search._area(("folder", "1")) == "kb:folder:1"  # pyright: ignore[reportPrivateUsage]
    for made_up in ("docs:collection:5", "notes:audience:5", "docs:audience"):
        with pytest.raises(ValueError, match="isn't an area"):
            search._area(made_up)  # pyright: ignore[reportPrivateUsage]


def test_an_ai_connection_carries_its_owners_groups() -> None:
    identity = Identity(sessionmaker=None)  # type: ignore[arg-type]
    token = SimpleNamespace(
        subject="u1", scopes=["reader"],
        claims={"upn": "pat@example.com", "name": "Pat", "groups": ["app-itops-security"]},
    )  # fmt: skip
    p = principal_from_access_token(token, identity)
    assert p.via_ai and p.groups == frozenset({"app-itops-security"})
