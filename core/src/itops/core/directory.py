"""Is a token's owner still allowed? Their account, checked against Active Directory.

Tokens (personal access tokens and OAuth tokens, ADR 0013) outlive a web sign-in, so when
someone leaves or loses their IT-Ops group, their tokens must stop working without waiting
for them to expire. ``identity.py`` asks this at most hourly per person, while their tokens
are in use: disabled, gone, or in none of the IT-Ops role groups means their tokens are
refused (and OAuth connections revoked); otherwise their roles are refreshed from AD.

It reads AD with ADSI as the service's own account (a domain gMSA can read users and
groups), in a short PowerShell process. Values reach it through environment variables and
are escaped for LDAP: nothing is pasted into the script. If AD can't be reached the answer
is "error" and the caller keeps what it knew (a DC outage mustn't lock everyone out).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger("itops.directory")
TIMEOUT_SECONDS = 20

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Esc([string] $v) {
    $v -replace '\\', '\5c' -replace '\*', '\2a' -replace '\(', '\28' -replace '\)', '\29' `
       -replace "`0", '\00'
}
$q = '(&(objectCategory=person)(objectClass=user)(userPrincipalName=' + (Esc $env:ITOPS_UPN) + '))'
$s = [adsisearcher]$q
[void]$s.PropertiesToLoad.Add('userAccountControl')
[void]$s.PropertiesToLoad.Add('accountExpires')
[void]$s.PropertiesToLoad.Add('distinguishedName')
$r = $s.FindOne()
if (-not $r) { 'STATUS missing'; exit 0 }
if (([int]$r.Properties['useraccountcontrol'][0]) -band 2) { 'STATUS disabled'; exit 0 }
# An account past its expiry date can't sign in either (0 or the maximum: never expires).
$exp = $r.Properties['accountexpires']
if ($exp.Count) {
    $v = [int64]$exp[0]
    if ($v -gt 0 -and $v -lt [int64]::MaxValue -and
        [DateTime]::FromFileTimeUtc($v) -lt [DateTime]::UtcNow) { 'STATUS disabled'; exit 0 }
}
$dn = Esc ([string]$r.Properties['distinguishedname'][0])
'STATUS active'
foreach ($g in ($env:ITOPS_GROUPS -split ',')) {
    if (-not $g) { continue }
    $gq = '(&(objectClass=group)(sAMAccountName=' + (Esc $g) + ')'
    $gq += '(member:1.2.840.113556.1.4.1941:=' + $dn + '))'
    if (([adsisearcher]$gq).FindOne()) { "GROUP $g" }
}
"""

Status = Literal["active", "disabled", "missing", "error"]


@dataclass(frozen=True)
class DirectoryAnswer:
    status: Status
    groups: list[str] = field(default_factory=list[str])  # the role groups they're in


DirectoryCheck = Callable[[str, list[str]], Awaitable[DirectoryAnswer]]


async def check_user(upn: str, groups: list[str]) -> DirectoryAnswer:
    """``upn``'s account in AD: active (and which of ``groups`` it's in, nested membership
    included), disabled, missing, or error (couldn't tell)."""
    env = {**os.environ, "ITOPS_UPN": upn, "ITOPS_GROUPS": ",".join(groups)}
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )  # fmt: skip
    except (OSError, NotImplementedError) as exc:  # no subprocesses on this event loop
        log.warning("directory check unavailable: %s", exc)
        return DirectoryAnswer("error")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        log.warning("directory check for %s timed out", upn)
        return DirectoryAnswer("error")
    if proc.returncode != 0:
        log.warning("directory check for %s failed: %s", upn, err.decode("utf-8", "replace")[-300:])
        return DirectoryAnswer("error")
    return parse(out.decode("utf-8", "replace"))


def parse(output: str) -> DirectoryAnswer:
    status: Status = "error"
    found: list[str] = []
    for line in output.splitlines():
        kind, _, value = line.strip().partition(" ")
        if kind == "STATUS" and value in ("active", "disabled", "missing"):
            status = value  # type: ignore[assignment]
        elif kind == "GROUP" and value:
            found.append(value)
    return DirectoryAnswer(status, found if status == "active" else [])
