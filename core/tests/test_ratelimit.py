"""Rate limits (core/ratelimit.py): sign-in endpoints per address, made-up tokens per
address, MCP/API per token; Cloudflare's address only believed from the tunnel host."""

from __future__ import annotations

import httpx
from starlette.types import Receive, Scope, Send

from itops.core.ratelimit import Limiter, Limits, RateLimit, Window, client_address

TUNNEL = "10.9.9.9"  # the Cloudflare Tunnel host, as the local proxy sees it
REAL = {"real", "stale"}  # tokens that exist (stale: expired or revoked, still answers 401)


async def backend(scope: Scope, receive: Receive, send: Send) -> None:
    """Answers 200 for the token "real", 401 for anything else."""
    headers = dict(scope["headers"])
    status = 200 if headers.get(b"authorization") in (None, b"Bearer real") else 401
    await send({"type": "http.response.start", "status": status, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


async def known(token: str) -> bool:
    return token in REAL


def limiter(**limits: int) -> Limiter:
    return Limiter(Limits(**limits), trusted_proxies=[TUNNEL], known=known)


def client(lim: Limiter) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=RateLimit(backend, lim), client=("127.0.0.1", 5000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def from_internet(ip: str, token: str | None = None) -> dict[str, str]:
    """Through Cloudflare and the tunnel, then the local proxy (which sets X-Forwarded-For)."""
    headers = {"x-forwarded-for": TUNNEL, "cf-connecting-ip": ip}
    return {**headers, "authorization": f"Bearer {token}"} if token else headers


def test_window_slides() -> None:
    w = Window(2, 60)
    assert w.hit("a", 0) and w.hit("a", 1) and not w.hit("a", 2)
    assert w.hit("b", 2)  # per key
    assert w.hit("a", 61)  # the first hit has left the window
    assert w.retry_after("a", 61) >= 1


def test_cloudflare_address_is_believed_from_the_tunnel_host_only() -> None:
    def scope(ip: str, headers: dict[str, str]) -> Scope:
        raw = [(k.encode(), v.encode()) for k, v in headers.items()]
        return {"type": "http", "client": (ip, 1), "headers": raw}

    trusted = [TUNNEL]
    cf = from_internet("203.0.113.9")
    assert client_address(scope("127.0.0.1", cf), trusted) == "203.0.113.9"
    # Someone on the network claiming an address: the proxy names them, CF header ignored.
    spoof = {"x-forwarded-for": "10.1.2.3", "cf-connecting-ip": "20.0.0.1"}
    assert client_address(scope("127.0.0.1", spoof), trusted) == "10.1.2.3"
    # The proxy appends its view; a caller-sent X-Forwarded-For in front changes nothing.
    chain = {"x-forwarded-for": f"{TUNNEL}, 10.1.2.3", "cf-connecting-ip": "20.0.0.1"}
    assert client_address(scope("127.0.0.1", chain), trusted) == "10.1.2.3"
    assert client_address(scope("10.1.2.3", cf), trusted) == "10.1.2.3"  # not via the proxy
    assert client_address(scope("127.0.0.1", {}), trusted) == ""  # the proxy didn't say
    # IIS ARR adds the caller's port.
    with_port = {"x-forwarded-for": f"{TUNNEL}:61785", "cf-connecting-ip": "203.0.113.9"}
    assert client_address(scope("127.0.0.1", with_port), trusted) == "203.0.113.9"
    v6 = {"x-forwarded-for": "[2001:db8::5]:443"}
    assert client_address(scope("127.0.0.1", v6), trusted) == "2001:db8::5"
    assert client_address(scope("127.0.0.1", {"x-forwarded-for": "2001:db8::5"})) == "2001:db8::5"


async def test_sign_in_endpoints_are_limited_per_address() -> None:
    lim = limiter(sign_in_per_minute=3)
    async with client(lim) as c:
        for _ in range(3):
            assert (await c.post("/token", headers=from_internet("203.0.113.9"))).status_code == 200
        refused = await c.post("/token", headers=from_internet("203.0.113.9"))
        assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
        assert refused.json()["error"] == "too_many_requests"
        assert (await c.post("/token", headers=from_internet("198.51.100.4"))).status_code == 200
        for _ in range(5):  # no address known: not lumped together
            assert (await c.post("/token")).status_code == 200
        assert (await c.post("/tokens", headers=from_internet("203.0.113.9"))).status_code == 200
    assert lim.refusals[0].rule == "sign-in" and lim.refusals[0].path == "/token"


async def test_made_up_tokens_hold_an_address_off_but_never_real_ones() -> None:
    lim = limiter(failed_tokens_per_5_minutes=3)
    async with client(lim) as c:
        ip = "20.0.0.1"  # e.g. OpenAI's, shared by everyone in ChatGPT
        for _ in range(10):  # stale tokens (about to refresh) don't count
            assert (await c.post("/mcp/", headers=from_internet(ip, "stale"))).status_code == 401
        assert (await c.post("/mcp/", headers=from_internet(ip, "real"))).status_code == 200
        for _ in range(3):  # made-up ones do
            assert (await c.post("/mcp/", headers=from_internet(ip, "guess"))).status_code == 401
        assert (await c.post("/mcp/", headers=from_internet(ip, "guess2"))).status_code == 429
        # Still open to anyone there with a real (or stale) token.
        assert (await c.post("/mcp/", headers=from_internet(ip, "real"))).status_code == 200
        assert (await c.post("/mcp/", headers=from_internet(ip, "stale"))).status_code == 401
        other = from_internet("198.51.100.4", "guess")
        assert (await c.post("/mcp/", headers=other)).status_code == 401
    assert lim.refusals[0].rule == "made-up tokens"


async def test_calls_are_limited_per_token_not_per_address() -> None:
    lim = limiter(token_per_minute=3)
    async with client(lim) as c:
        for _ in range(3):
            r = await c.post("/mcp/", headers=from_internet("20.0.0.1", "real"))
            assert r.status_code == 200
        assert (await c.post("/mcp/", headers=from_internet("20.0.0.1", "real"))).status_code == 429
        assert (
            await c.post("/mcp/", headers=from_internet("20.0.0.1", "other"))
        ).status_code == 401
    assert lim.refusals[0].client.startswith("token ")  # a fingerprint, never the token
