"""Shared test helpers (importable because pytest adds core/tests to sys.path)."""

from __future__ import annotations

from collections.abc import Callable

from itops.sdk import Module

MakeModule = Callable[..., Module]

TOOL_BODY = '''
@module.tool(permission="{name}.read")
async def ping() -> str:
    """Say pong."""
    return "pong"
'''


def perms(name: str) -> str:
    return f'[permissions]\n"{name}.read" = "Read {name}"\n'
