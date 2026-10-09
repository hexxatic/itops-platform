"""Principals and permissions.

Every request (web, API, MCP) acts as a :class:`Principal`. ``dev`` mode (development
only): everyone is a local all-permissions user. ``entra`` mode: people sign in with
Entra ID; roles come from their groups (:mod:`itops.core.identity`); AI tools and
scripts use personal access tokens. Anything unauthenticated is refused (fail closed).

**AI drafts, humans approve (ADR 0003).** Requests arriving through MCP act as the
connected user *via AI*. Permissions a manifest marks ``human_only`` (``docs.publish``)
are never granted to an AI principal, whatever the user's role, so an AI can create
drafts but a person always publishes.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace

from itops.core.config import Settings

ALL = "*"


@dataclass(frozen=True)
class Principal:
    user: str
    display_name: str
    permissions: frozenset[str]
    via_ai: bool = False
    client: str = "web"  # "web" | "api" | "mcp" | "connector" (audit attribution)

    def has(self, permission: str) -> bool:
        """May this connection exercise ``permission``? Never a human-only one via AI."""
        if self.via_ai and permission in _human_only:
            return False
        return self.holds(permission)

    def holds(self, permission: str) -> bool:
        """Does the person behind this connection hold ``permission``, whatever the
        channel? For seeing things and asking for them (an AI may request a tool run its
        user could start), never for doing a human-only thing."""
        return ALL in self.permissions or permission in self.permissions


DEV_PRINCIPAL = Principal(user="dev", display_name="Local developer", permissions=frozenset({ALL}))

_current: ContextVar[Principal | None] = ContextVar("itops_principal", default=None)


class _State:
    dev_mode: bool = False
    mcp_resolver: Callable[[], Principal | None] | None = None


_state = _State()
_human_only: set[str] = set()


class PermissionDenied(PermissionError):
    pass


def configure(settings: Settings) -> None:
    if settings.auth_mode == "entra":
        missing = [
            name
            for name, value in (
                ("ITOPS_ENTRA_TENANT_ID", settings.entra_tenant_id),
                ("ITOPS_ENTRA_CLIENT_ID", settings.entra_client_id),
                ("ITOPS_ENTRA_CLIENT_SECRET", settings.entra_client_secret.get_secret_value()),
            )
            if not value
        ]
        if not settings.is_dev and not settings.secret_key.get_secret_value():
            missing.append("ITOPS_SECRET_KEY")
        if missing:
            raise RuntimeError("Entra sign-in needs: " + ", ".join(missing))
        _state.dev_mode = False
        return
    if not settings.is_dev:
        raise RuntimeError("ITOPS_AUTH_MODE=dev is only allowed with ITOPS_ENV=development.")
    _state.dev_mode = True


def set_mcp_resolver(resolver: Callable[[], Principal | None] | None) -> None:
    """Entra mode: how to find the principal of an MCP call (from the SDK's verified
    access token, which lives in the SDK's own request context)."""
    _state.mcp_resolver = resolver


def set_human_only(permissions: set[str]) -> None:
    """Called by the core at load with every module's human-only permissions."""
    _human_only.clear()
    _human_only.update(permissions)


def is_human_only(permission: str) -> bool:
    return permission in _human_only


def set_current(principal: Principal | None) -> None:
    _current.set(principal)


def set_current_token(principal: Principal | None) -> Token[Principal | None]:
    """Act as ``principal`` until :func:`reset_current` (the core: connector runs)."""
    return _current.set(principal)


def reset_current(token: Token[Principal | None]) -> None:
    _current.reset(token)


def current() -> Principal | None:
    """The acting principal.

    Web and API requests set it in middleware. MCP tool calls run in the SDK's own
    tasks, where that context variable doesn't reach: dev mode falls back to the dev
    user, Entra mode asks the MCP resolver (the verified personal access token).
    """
    p = _current.get()
    if p is not None:
        return p
    if _state.dev_mode:
        return DEV_PRINCIPAL
    return _state.mcp_resolver() if _state.mcp_resolver else None


@contextmanager
def acting_via_ai(client: str = "mcp") -> Generator[Principal | None]:
    """Run a block as the current user *via AI* (MCP tool calls)."""
    p = current()
    ai = replace(p, via_ai=True, client=client) if p else None
    token = _current.set(ai)
    try:
        yield ai
    finally:
        _current.reset(token)


def require(permission: str) -> Principal:
    p = current()
    if p is not None and p.via_ai and permission in _human_only:
        raise PermissionDenied(
            f"{permission!r} is human-only: an AI connection can create drafts, but a "
            "person has to do this in the web UI."
        )
    if p is None or not p.has(permission):
        who = p.user if p else "anonymous"
        raise PermissionDenied(f"{who} lacks permission {permission!r}")
    return p
