"""``/ai-connections``: every AI tool connected to IT-Ops, for admins (ADR 0013).

Who connected which tool (OAuth, like ChatGPT) or holds a personal access token, when each
was last used, with a button to cut any of them off; and the requests the rate limiter
refused lately (``ratelimit.py``)."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.audit import AUDIT_PERMISSION
from itops.core.identity import Identity
from itops.core.oauth import OAuthProvider
from itops.core.ratelimit import Limiter

AI_CONNECTIONS_PERMISSION = AUDIT_PERMISSION  # oversight, like the audit trail: admins


def _when(value: dt.datetime | None, never: str) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if value else never


def mount_ai_connections(
    app: FastAPI,
    templates: Jinja2Templates,
    identity: Identity,
    oauth: OAuthProvider | None,
    limiter: Limiter | None,
) -> None:
    def admin() -> None:
        """Admins, in a browser: a token (an AI tool) mustn't list or cut off others' access."""
        try:
            principal = auth.require(AI_CONNECTIONS_PERMISSION)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail="This page is for admins.") from exc
        if principal.via_ai:
            raise HTTPException(status_code=403, detail="Open this page in a browser.")

    @app.get("/ai-connections", response_class=HTMLResponse, include_in_schema=False)
    async def page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        admin()
        connections = await oauth.all_connections() if oauth else []
        tokens = await identity.all_tokens()
        return templates.TemplateResponse(
            request,
            "core/ai_connections.html",
            {
                "page_title": "AI connections",
                "oauth_on": oauth is not None,
                "connection_rows": [
                    {
                        "person": c["person"],
                        "client": c["client"],
                        "via": c["redirect_host"],
                        "connected": _when(c["created"], ""),
                        "used": _when(c["last_used"], "Not yet"),
                        "revoke": {
                            "action": f"/ai-connections/oauth/{c['grant_id']}/revoke",
                            "label": "Disconnect",
                            "variant": "danger",
                            "confirm": f"Disconnect {c['person']}'s {c['client']}? It stops"
                            " working until they connect it again.",
                        },
                    }
                    for c in connections
                ],
                "token_rows": [
                    {
                        "person": t["person"],
                        "name": t["name"],
                        "prefix": t["prefix"] + "…",
                        "created": _when(t["created"], ""),
                        "expires": f"{t['expires']:%Y-%m-%d}",
                        "used": _when(t["last_used"], "Never"),
                        "revoke": {
                            "action": f"/ai-connections/tokens/{t['id']}/revoke",
                            "label": "Revoke",
                            "variant": "danger",
                            "confirm": f"Revoke {t['person']}'s token “{t['name']}”? Tools"
                            " using it stop working.",
                        },
                    }
                    for t in tokens
                ],
                "refusal_rows": [
                    {
                        "at": f"{r.at:%Y-%m-%d %H:%M:%S} UTC",
                        "rule": r.rule,
                        "client": r.client,
                        "path": r.path,
                    }
                    for r in (list(limiter.refusals)[:50] if limiter else [])
                ],
            },
        )

    @app.post("/ai-connections/oauth/{grant_id}/revoke", include_in_schema=False)
    async def disconnect(grant_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        admin()
        try:
            uuid.UUID(grant_id)
        except ValueError as exc:
            raise HTTPException(status_code=404) from exc
        if oauth is None:
            raise HTTPException(status_code=404)
        await oauth.revoke_any_grant(grant_id)
        return RedirectResponse("/ai-connections", status_code=303)

    @app.post("/ai-connections/tokens/{token_id}/revoke", include_in_schema=False)
    async def revoke(token_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        admin()
        await identity.revoke_any_token(token_id)
        return RedirectResponse("/ai-connections", status_code=303)
