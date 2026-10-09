"""Tool actions (ADR 0011): readable failures from remote endpoints."""

from __future__ import annotations

from itops.core.actions import _remote_failure  # pyright: ignore[reportPrivateUsage]

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
