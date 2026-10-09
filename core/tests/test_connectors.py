"""Connectors (ADR 0010): schedules, declarations, secrets. (Runs end to end:
tests/integration/test_connectors.py.)"""

from __future__ import annotations

import datetime as dt

import pytest
from _helpers import MakeModule

from itops.core.connectors import ConnectorContext, ConnectorError, is_due, next_due
from itops.core.loader import validate
from itops.core.manifest import ConnectorDecl

NOW = dt.datetime(2026, 10, 7, 12, 0)


def test_every_schedules() -> None:
    assert is_due("every 30m", None, NOW)  # never ran: due now
    assert not is_due("every 30m", NOW - dt.timedelta(minutes=10), NOW)
    assert is_due("every 30m", NOW - dt.timedelta(minutes=30), NOW)
    assert next_due("every 6h", NOW, NOW) == NOW + dt.timedelta(hours=6)
    assert next_due("every 1d", NOW, NOW) == NOW + dt.timedelta(days=1)
    assert next_due("manual", None, NOW) is None
    assert not is_due("manual", None, NOW)


def test_daily_schedule_runs_once_a_day() -> None:
    slot = next_due("daily 02:00", None, NOW)
    assert slot is not None
    # Ran at today's slot: next one is tomorrow's.
    assert next_due("daily 02:00", slot, NOW) == slot + dt.timedelta(days=1)
    assert is_due("daily 02:00", slot - dt.timedelta(days=1), slot + dt.timedelta(minutes=1))
    assert not is_due("daily 02:00", slot, slot + dt.timedelta(minutes=1))


@pytest.mark.parametrize(
    ("field", "value"),
    [("schedule", "hourly"), ("schedule", "every 5s"), ("direction", "sideways"),
     ("owns", {"device": ["name"]}), ("secrets", ["Api-Key"]), ("missing", "delete")],
)  # fmt: skip
def test_bad_declarations_are_refused(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        ConnectorDecl.model_validate({"title": "X", "system": "Y", field: value})


def test_declared_and_registered_connectors_must_match(make_module: MakeModule) -> None:
    body = (
        "from itops.sdk import ConnectorContext\n"
        '@module.connector("pull")\n'
        "async def pull(ctx: ConnectorContext) -> None:\n"
        "    pass\n"
    )
    declared = '[connectors.pull]\ntitle = "Pull"\nsystem = "Somewhere"\n'
    assert validate(make_module("alpha", manifest_extra=declared, body=body)) == []
    problems = validate(make_module("beta", body=body.replace("alpha", "beta")))
    assert any("not declared in [connectors]" in p for p in problems)
    problems = validate(make_module("gamma", manifest_extra=declared))
    assert any("no @module.connector('pull')" in p for p in problems)


def test_secrets_come_from_the_environment_and_must_be_declared() -> None:
    decl = ConnectorDecl(title="IT Glue import", system="IT Glue", secrets=["api_key"])
    env = {"ITOPS_ITGLUE_API_KEY": "k"}
    ctx = ConnectorContext(key="itglue.itglue", decl=decl, run_id=1, dry_run=True, secrets=env.get)
    assert ctx.secret("api_key") == "k"
    with pytest.raises(ConnectorError, match="isn't declared"):
        ctx.secret("password")
    empty = ConnectorContext(key="itglue.itglue", decl=decl, run_id=1, dry_run=True,
                             secrets=lambda _name: None)  # fmt: skip
    with pytest.raises(ConnectorError, match="ITOPS_ITGLUE_API_KEY isn't set"):
        empty.secret("api_key")


def test_problems_are_capped() -> None:
    ctx = ConnectorContext(key="a.b", decl=ConnectorDecl(title="T", system="S"), run_id=1,
                           dry_run=False, secrets=lambda _n: None)  # fmt: skip
    for i in range(600):
        ctx.problem(f"problem {i}")
    assert len(ctx.problems) == 501 and ctx.problems[-1]["text"] == "More problems were left out."
    ctx.apply({"outcome": "failed", "problem": "bad"}, item="x")
    assert ctx.counts == {"failed": 1}
