"""Tool actions (ADR 0011): readable failures from remote endpoints."""

from __future__ import annotations

import pytest

from itops.core.actions import (
    ActionContext,
    ActionError,
    _remote_failure,  # pyright: ignore[reportPrivateUsage]
    declared,
)
from itops.core.manifest import ActionDecl

DENIED = (
    "Connecting to remote server DC01.CORP.EXAMPLE.COM failed with the following "
    "error message : Access is denied. For more information, see the about_Remote_Troubleshooting "
    "Help topic. At line:1 char:34 + ... e = 'Stop'; Invoke-Command -ComputerName 'DC01' ... "
    "+ CategoryInfo : OpenError: (DC01:String) [], PSRemotingTransportException "
    "+ FullyQualifiedErrorId : AccessDenied,PSSessionStateBroken"
)
HOST = "DC01.CORP.EXAMPLE.COM"


def test_access_denied_says_why_in_plain_words() -> None:
    msg = _remote_failure("Sync-ITOpsDomain", HOST, "ITOps.ADReplication", DENIED, 1)
    assert msg.startswith("DC01 refused this IT-Ops server's account on the ITOps.ADReplication")
    assert "expected on test" in msg and "At line" not in msg and "CategoryInfo" not in msg


def test_other_failures_lose_the_powershell_noise() -> None:
    err = (
        "Connecting to remote server X failed with the following error message : The client "
        "cannot connect to the destination specified. For more information, see the "
        "about_Remote_Troubleshooting Help topic. At line:1 char:2 + junk"
    )
    msg = _remote_failure("Sync-ITOpsDomain", HOST, "ITOps.ADReplication", err, 1)
    assert msg == (
        "Sync-ITOpsDomain on DC01 failed: The client cannot connect to the destination specified."
    )
    missing = "The term 'Sync-ITOpsDomain' is not recognized as the name of a cmdlet, function"
    assert "is the endpoint up to date" in _remote_failure(
        "Sync-ITOpsDomain", HOST, "E", missing, 1
    )
    assert _remote_failure("C", HOST, "E", "", 3) == "C on DC01 failed: exit code 3"


def test_declared_endpoints_match_short_or_full_names() -> None:
    eps = ["DC01/ITOps.ADReplication", "sync.corp.example.com/ITOps.EntraSync"]
    assert declared(eps, "DC01", "ITOps.ADReplication")
    assert declared(eps, "dc01.corp.example.com", "itops.adreplication")
    assert declared(eps, "SYNC.corp.example.com", "ITOps.EntraSync")
    assert not declared(eps, "sync", "ITOps.EntraSync")  # declared fully qualified only
    assert not declared(eps, "DC02.corp.example.com", "ITOps.ADReplication")
    assert not declared(eps, "DC01", "ITOps.EntraSync")
    assert not declared([], "DC01", "ITOps.ADReplication")


async def test_undeclared_endpoints_are_refused_before_anything_runs() -> None:
    started: list[object] = []

    async def spawn(*args: object, **kwargs: object) -> object:
        started.append(args)
        raise AssertionError("must not start PowerShell")

    decl = ActionDecl(title="t", permission="x.run", endpoints=["DC01/ITOps.Fake"])
    ctx = ActionContext(key="x.t", decl=decl, run_id=1, inputs={}, secrets=lambda _: None)
    original = ActionContext.spawn
    ActionContext.spawn = spawn  # type: ignore[assignment]
    try:
        with pytest.raises(ActionError, match="declared endpoints"):
            await ctx.powershell("DC02.corp.example.com", "ITOps.Fake", "Get-Thing")
        with pytest.raises(ActionError, match="declared endpoints"):
            await ctx.powershell("DC01", "ITOps.Other", "Get-Thing")
    finally:
        ActionContext.spawn = original  # type: ignore[assignment]
    assert started == []
