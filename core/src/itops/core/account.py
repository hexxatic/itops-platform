"""Sign-in routes (Entra mode) and the account page: who you are, your roles, and your
personal access tokens for AI tools and scripts."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.config import Settings
from itops.core.entra import EntraAuth, session_user
from itops.core.identity import MAX_TOKEN_DAYS, Identity, User, top_role
from itops.core.oauth import OAuthProvider
from itops.core.theme import APPEARANCES, current_appearance, parse_appearance


def mount_account(
    app: FastAPI,
    templates: Jinja2Templates,
    settings: Settings,
    identity: Identity,
    admin_links: list[dict[str, str]] | None = None,
    oauth: OAuthProvider | None = None,
) -> None:
    """``admin_links``: [{label, href, permission}], shown to people holding the permission.
    ``oauth``: AI tools connected with OAuth (ADR 0013), listed and revocable here."""
    links = admin_links or []

    def message(request: Request, title: str, detail: str, status: int = 403) -> Response:
        return templates.TemplateResponse(
            request,
            "core/message.html",
            {"page_title": title, "title": title, "detail": detail},
            status_code=status,
        )

    entra = settings.auth_mode == "entra"
    if entra:
        flow = EntraAuth(settings, identity, message)
        app.add_api_route("/auth/login", flow.login, methods=["GET"], include_in_schema=False)
        app.add_api_route("/auth/callback", flow.callback, methods=["GET"], include_in_schema=False)
        app.add_api_route("/auth/logout", flow.logout, methods=["GET"], include_in_schema=False)

    def signed_in_user(request: Request) -> User:
        user = session_user(request.scope)
        if user is None:
            raise HTTPException(status_code=401)
        return user

    async def render(request: Request, new_token: str | None = None) -> Response:
        principal = auth.current()
        user = session_user(request.scope) if entra else None
        tokens = await identity.tokens(user.id) if user else []
        connections = await oauth.connections(user.id) if (oauth and user) else []
        return templates.TemplateResponse(
            request,
            "core/account.html",
            {
                "page_title": "Your account",
                "principal": principal,
                "user": user,
                "role": top_role(user.roles) if user else "admin (dev mode)",
                "entra": entra,
                "token_rows": [
                    {
                        "name": t.name,
                        "prefix": t.prefix + "…",
                        "created": f"{t.created_at:%Y-%m-%d}",
                        "expires": f"{t.expires_at:%Y-%m-%d}",
                        "used": f"{t.last_used_at:%Y-%m-%d %H:%M}" if t.last_used_at else "Never",
                        "revoke": {
                            "action": f"/account/tokens/{t.id}/revoke",
                            "label": "Revoke",
                            "variant": "danger",
                            "confirm": "Revoke this token? Tools using it stop working.",
                        },
                    }
                    for t in tokens
                ],
                "new_token": new_token,
                "oauth_on": oauth is not None,
                "connection_rows": [
                    {
                        "client": c["client"],
                        "via": c["redirect_host"],
                        "connected": f"{c['created']:%Y-%m-%d}",
                        "used": f"{c['last_used']:%Y-%m-%d %H:%M}" if c["last_used"] else "Not yet",
                        "revoke": {
                            "action": f"/account/connections/{c['grant_id']}/revoke",
                            "label": "Disconnect",
                            "variant": "danger",
                            "confirm": f"Disconnect {c['client']}? It stops working until"
                            " you connect it again.",
                        },
                    }
                    for c in connections
                ],
                "chosen_appearance": current_appearance(),
                "appearances": [("auto", "Match my system"), ("light", "Light"), ("dark", "Dark")],
                "max_days": MAX_TOKEN_DAYS,
                "mcp_url": settings.public_url.rstrip("/") + "/mcp/",
                "admin_links": links,
                "allowed": [
                    x["permission"] for x in links if principal and principal.has(x["permission"])
                ],
            },
        )

    @app.get("/account", response_class=HTMLResponse, include_in_schema=False)
    async def account(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await render(request)

    @app.post("/account/appearance", include_in_schema=False)
    async def set_appearance(  # pyright: ignore[reportUnusedFunction]
        request: Request, appearance: Annotated[str, Form()] = "auto"
    ) -> Response:
        if appearance not in APPEARANCES:
            raise HTTPException(status_code=422, detail="Choose auto, light or dark.")
        request.session["appearance"] = parse_appearance(appearance)
        user = session_user(request.scope) if entra else None
        if user:  # dev mode has no user rows: the session alone remembers it
            await identity.set_appearance(user, appearance)
        return RedirectResponse("/account", status_code=303)

    @app.post("/account/tokens", response_class=HTMLResponse, include_in_schema=False)
    async def create_token(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        name: Annotated[str, Form()] = "",
        days: Annotated[int, Form()] = MAX_TOKEN_DAYS,
    ) -> Response:
        if not entra:
            raise HTTPException(status_code=404, detail="Dev mode doesn't use tokens.")
        user = signed_in_user(request)
        token = await identity.create_token(user, name, days)
        # Shown once, in this response only; never stored or logged.
        return await render(request, new_token=token)

    @app.post("/account/connections/{grant_id}/revoke", include_in_schema=False)
    async def disconnect(request: Request, grant_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        user = signed_in_user(request)
        try:
            uuid.UUID(grant_id)
        except ValueError as exc:
            raise HTTPException(status_code=404) from exc
        if oauth is None:
            raise HTTPException(status_code=404)
        await oauth.revoke_grant(user.id, grant_id)
        return RedirectResponse("/account", status_code=303)

    @app.post("/account/tokens/{token_id}/revoke", include_in_schema=False)
    async def revoke_token(request: Request, token_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        user = signed_in_user(request)
        await identity.revoke_token(user, token_id)
        return RedirectResponse("/account", status_code=303)
