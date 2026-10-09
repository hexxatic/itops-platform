"""Rate limits for the surfaces AI clients and the internet can reach (ADR 0013).

Three limits, all per minute or so, held in memory (one process per environment; a restart
starts the counts again, which is fine for this):

- **Sign-in endpoints** (``/register``, ``/authorize``, ``/token``, ``/revoke``): per client
  address. These are the ones worth hammering, and nobody needs many.
- **Made-up tokens**: a client address that keeps presenting tokens that never existed is
  held off for a while (guessing). Stale tokens (expired, revoked: an AI client about to
  refresh, a leaver's tool retrying) don't count, and a held-off address still gets in with
  a real token, so one address can't lock out everyone behind it (all of ChatGPT).
- **MCP and the API with a token**: per token, generous. Not per address: everything from
  ChatGPT arrives from a few OpenAI addresses, so one person's busy chat mustn't slow
  everyone else's.

The client address: a request straight to the service is its own address. Through the local
reverse proxy (loopback), it's ``X-Forwarded-For``, which the proxy must *set* to the
caller's address, overwriting whatever the caller sent (the IIS site does). Only when that
caller is a trusted proxy, like the Cloudflare Tunnel host (``ITOPS_TRUSTED_PROXIES``), is
Cloudflare's ``CF-Connecting-IP`` believed; from anyone else it's ignored, so nobody can
claim another address. No address known (a proxy that doesn't say): the per-address limits
skip the request rather than lump everyone together; the per-token limit still applies.

Refusals are logged and kept (the last few hundred) for the admin page ``/ai-connections``.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("itops.ratelimit")

SIGN_IN_PATHS = ("/register", "/authorize", "/token", "/revoke")
TOKEN_PATHS = ("/mcp", "/api")
LOOPBACK = ("127.0.0.1", "::1")
MAX_KEYS = 20_000  # bound the memory a flood of addresses can take


@dataclass(frozen=True)
class Limits:
    sign_in_per_minute: int = 30
    failed_tokens_per_5_minutes: int = 20
    token_per_minute: int = 600


@dataclass(frozen=True)
class Refusal:
    at: dt.datetime
    rule: str  # "sign-in", "made-up tokens", "token"
    client: str  # an address, or a token's short fingerprint
    path: str


class Window:
    """Sliding-window counters: ``hit(key)`` records one and says whether it was within
    ``limit`` per ``seconds``; ``over(key)`` records nothing."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit = limit
        self.seconds = seconds
        self._hits: collections.OrderedDict[str, collections.deque[float]] = (
            collections.OrderedDict()
        )

    def _recent(self, key: str, now: float) -> collections.deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = collections.deque()
            if len(self._hits) > MAX_KEYS:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        while hits and hits[0] <= now - self.seconds:
            hits.popleft()
        return hits

    def hit(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._recent(key, now)
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def over(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return len(self._recent(key, now)) >= self.limit

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        hits = self._recent(key, now)
        return max(1, int(hits[0] + self.seconds - now) + 1) if hits else 1


def _under(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _without_port(address: str) -> str:
    """``1.2.3.4:5678`` -> ``1.2.3.4``; ``[::1]:5678`` -> ``::1`` (IIS ARR adds the port)."""
    if address.startswith("["):
        return address[1:].split("]")[0]
    return address.split(":")[0] if address.count(":") == 1 else address


def client_address(scope: Scope, trusted: Iterable[str] = ()) -> str:
    client = scope.get("client")
    direct = client[0] if client else ""
    if direct not in LOOPBACK:
        return direct
    headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
    forwarded = headers.get("x-forwarded-for", "").split(",")[-1].strip()  # the proxy's view
    caller = _without_port(forwarded)
    if caller and caller in set(trusted):
        return headers.get("cf-connecting-ip", "").strip()
    return caller  # "" when the proxy didn't say


def _bearer(scope: Scope) -> str:
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            value = v.decode("latin-1")
            if value.lower().startswith("bearer "):
                return value[7:].strip()
    return ""


def fingerprint(token: str) -> str:
    return "token " + hashlib.sha256(token.encode()).hexdigest()[:8]


class Limiter:
    """The counters and the recent refusals, shared by the middleware and the admin page.
    ``known(token)``: was it ever a real token? (None: every bad token counts.)"""

    def __init__(
        self,
        limits: Limits | None = None,
        *,
        trusted_proxies: Iterable[str] = (),
        known: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self.trusted_proxies = frozenset(trusted_proxies)
        self.known = known
        self.sign_in = Window(self.limits.sign_in_per_minute, 60)
        self.failed = Window(self.limits.failed_tokens_per_5_minutes, 300)
        self.tokens = Window(self.limits.token_per_minute, 60)
        self.refusals: collections.deque[Refusal] = collections.deque(maxlen=300)


class RateLimit:
    """Pure ASGI middleware, outermost: refuses before any work is done."""

    def __init__(self, app: ASGIApp, limiter: Limiter) -> None:
        self.app = app
        self.limiter = limiter
        self.sign_in = limiter.sign_in
        self.failed = limiter.failed
        self.tokens = limiter.tokens
        self.refusals = limiter.refusals

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        address = client_address(scope, self.limiter.trusted_proxies)
        token = _bearer(scope)

        if address and _under(path, SIGN_IN_PATHS) and not self.sign_in.hit(address):
            await self._refuse(scope, receive, send, "sign-in", address, self.sign_in, address)
            return
        if token and _under(path, TOKEN_PATHS):
            if address and self.failed.over(address) and not await self._known(token):
                await self._refuse(scope, receive, send, "made-up tokens", address,
                                   self.failed, address)  # fmt: skip
                return
            key = fingerprint(token)
            if not self.tokens.hit(key):
                await self._refuse(scope, receive, send, "token", key, self.tokens, key)
                return

            async def watch(message: Message) -> None:
                if (
                    address
                    and message["type"] == "http.response.start"
                    and message["status"] == 401
                    and not await self._known(token)
                ):
                    self.failed.hit(address)
                await send(message)

            await self.app(scope, receive, watch)
            return
        await self.app(scope, receive, send)

    async def _known(self, token: str) -> bool:
        known = self.limiter.known
        if known is None:
            return False
        try:
            return await known(token)
        except Exception:  # the database is down: don't add a second failure on top
            log.exception("rate limit: couldn't check a token")
            return True

    async def _refuse(
        self, scope: Scope, receive: Receive, send: Send, rule: str, client: str,
        window: Window, key: str,
    ) -> None:  # fmt: skip
        path: str = scope["path"]
        log.warning("rate limit (%s) refused %s for %s", rule, path, client)
        self.refusals.appendleft(Refusal(dt.datetime.now(dt.UTC), rule, client, path))
        wait = window.retry_after(key)
        body = {"error": "too_many_requests", "error_description": f"Too many requests. "
                f"Try again in {wait} seconds."}  # fmt: skip
        await JSONResponse(body, 429, headers={"Retry-After": str(wait)})(scope, receive, send)
