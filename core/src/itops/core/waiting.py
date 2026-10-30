"""What's waiting for the person looking: AI drafts and proposals to approve, reviews and
decisions due. Each module offers a ``waiting`` service (``async def waiting() ->
[{title, url, count}]`` for the current principal); core gathers them for the home page's
*Waiting for you* list and the count on each module's nav item.

Gathered once a minute per person at most, every module at once, and again as soon as that
person changes something (any request of theirs that isn't a GET, AI's included), so what
they approve or propose shows at once; what others change shows within the minute.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from itops.core import auth
from itops.core.problems import local_url

log = logging.getLogger("itops.waiting")

TTL_SECONDS = 60
# Paths that never show the shell: no need to gather anything for them.
NO_SHELL = ("/api/", "/static/", "/files/", "/mcp", "/theme/", "/auth/", "/oauth", "/.well-known",
            "/manifest.webmanifest", "/favicon.ico")  # fmt: skip


@dataclass(frozen=True)
class Waiting:
    module: str
    title: str  # what waits, with its count ("3 proposed asset changes")
    url: str  # where to act
    count: int


class WaitingBoard:
    """Every module's ``waiting`` items for the current person, cached briefly."""

    def __init__(
        self,
        services: Callable[[str], Any],
        modules: list[str],
        core: dict[str, Callable[[], Awaitable[list[dict[str, Any]]]]] | None = None,
    ) -> None:
        self._services = services
        self._modules = modules
        self._core = core or {}  # core's own (the Tools page's run requests)
        self._cache: dict[str, tuple[float, list[Waiting]]] = {}

    def clear(self, user: str | None = None) -> None:
        """Forget what was gathered for ``user`` (everyone when ``None``)."""
        if user is None:
            self._cache.clear()
        else:
            self._cache.pop(user, None)

    async def for_current(self) -> list[Waiting]:
        who = auth.current()
        if who is None or who.via_ai:
            return []
        hit = self._cache.get(who.user)
        if hit is not None and time.monotonic() - hit[0] < TTL_SECONDS:
            return hit[1]
        sources = [(n, self._services(f"{n}.waiting")) for n in self._modules]
        sources = [(n, fn) for n, fn in [*sources, *self._core.items()] if fn is not None]
        answers = await asyncio.gather(*(fn() for _, fn in sources), return_exceptions=True)
        found: list[Waiting] = []
        for (name, _), items in zip(sources, answers, strict=True):
            if isinstance(items, BaseException):  # one module's trouble mustn't break pages
                if not isinstance(items, Exception):
                    raise items
                log.error("%s.waiting failed", name, exc_info=items)
                continue
            for item in items:
                count = int(item.get("count") or 0)
                url = local_url(str(item.get("url") or ""))
                if count > 0 and url:  # "module": the nav item it counts under, if not this
                    module = str(item.get("module") or name)
                    found.append(Waiting(module, str(item["title"]), url, count))
        self._cache[who.user] = (time.monotonic(), found)
        return found


_counts: ContextVar[dict[str, int] | None] = ContextVar("itops_waiting_counts", default=None)


def nav_counts() -> dict[str, int]:
    """{module: how many things wait there} for this page's nav (a template global)."""
    return _counts.get() or {}


class WaitingMiddleware:
    """Pure ASGI, inside the auth gate: gathers the counts for pages that show the shell,
    and clears the board after any change."""

    def __init__(self, app: ASGIApp, board: WaitingBoard) -> None:
        self.app = app
        self.board = board

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if scope.get("method") != "GET":
            try:
                await self.app(scope, receive, send)
            finally:
                who = auth.current()
                if who is not None:
                    self.board.clear(who.user)
            return
        counts: dict[str, int] = {}
        if not path.startswith(NO_SHELL):
            for w in await self.board.for_current():
                counts[w.module] = counts.get(w.module, 0) + w.count
        token = _counts.set(counts)
        try:
            await self.app(scope, receive, send)
        finally:
            _counts.reset(token)
