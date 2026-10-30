"""Cross-site request forgery: changes come from IT-Ops' own pages.

The session cookie is ``SameSite=lax``, but "site" means the registrable domain: every
host under the company's domain counts as the same site, so a page on any of them could
post a form to IT-Ops with the signed-in person's cookie. :class:`SameOrigin` refuses a
request that changes something (any method but GET, HEAD, OPTIONS) and that rides on
ambient sign-in (the session cookie; in dev mode, every request) when the browser says it
came from another origin:

* ``Sec-Fetch-Site`` (every current browser sends it): only ``same-origin``, or ``none``
  (typed into the address bar, a bookmark);
* without it, an ``Origin`` header must be IT-Ops' own (the name the browser used, behind
  the reverse proxy too, or ``ITOPS_PUBLIC_URL``). No header at all is a script or an old
  browser, not a cross-site page, and passes.

Not affected: bearer tokens (a page can't make a browser send one to another site), and the
MCP surface (``/mcp``, the OAuth endpoints, ADR 0013), whose callers are other servers. The
Entra sign-in callback is a GET (query response mode), so it isn't a change either.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from itops.core.config import Settings
from itops.core.entra import is_mcp_path, public_host

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SESSION_COOKIE = "itops_session"
REFUSED = (
    "Refused: this request came from another site, not from an IT-Ops page. Open IT-Ops and "
    "try again there."
)


class SameOrigin:
    """Pure ASGI middleware; sits outside the session and the sign-in gate."""

    def __init__(self, app: ASGIApp, settings: Settings, hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.dev = settings.auth_mode == "dev"  # everyone is signed in: no cookie needed
        # IT-Ops' own name and its sites' (ADR 0027).
        self.own_hosts = {urlsplit(settings.public_url).netloc.lower(), *hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._allowed(scope):
            await self.app(scope, receive, send)
            return
        await PlainTextResponse(REFUSED, status_code=403)(scope, receive, send)

    def _allowed(self, scope: Scope) -> bool:
        request = Request(scope)
        if request.method in SAFE_METHODS or is_mcp_path(scope["path"]):
            return True
        if request.headers.get("authorization", "").lower().startswith("bearer "):
            return True
        if not self.dev and SESSION_COOKIE not in request.cookies:
            return True  # nobody is signed in on this request: nothing to forge
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site is not None:
            return fetch_site in ("same-origin", "none")
        origin = request.headers.get("origin")
        if origin is None:
            return True
        host = urlsplit(origin).netloc.lower()
        return bool(host) and host in self.own_hosts | {public_host(scope, request).lower()}
