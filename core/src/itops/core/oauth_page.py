"""The consent step of OAuth for MCP clients (ADR 0013): ``/oauth/consent``.

The authorization endpoint sends the browser here; the usual Entra sign-in comes first (the
page isn't public). It says which AI client wants to act as you, *via AI*, and what that
means. Allow sends the browser back to the client with a one-time code; Deny sends it back
with ``access_denied``. Only a signed-in browser session can decide, never a token.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core.config import Settings
from itops.core.entra import external_redirect, session_user
from itops.core.identity import User
from itops.core.oauth import CONSENT_PATH, OAuthProvider


def mount_oauth(
    app: FastAPI, templates: Jinja2Templates, settings: Settings, oauth: OAuthProvider
) -> None:
    def browser_user(request: Request) -> User:
        user = session_user(request.scope)
        if user is None or request.headers.get("authorization"):
            raise HTTPException(status_code=403, detail="Approve this in your browser.")
        return user

    def expired(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "core/message.html",
            {
                "page_title": "Sign-in request expired",
                "title": "This sign-in request has expired",
                "detail": "Go back to the AI tool and connect IT-Ops again.",
            },
            status_code=400,
        )

    @app.get(CONSENT_PATH, response_class=HTMLResponse, include_in_schema=False)
    async def consent_page(request: Request, request_id: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        user = browser_user(request)
        found = await oauth.pending(request.query_params.get("request", request_id))
        if found is None:
            return expired(request)
        client, params = found
        page = templates.TemplateResponse(
            request,
            "core/oauth_consent.html",
            {
                "page_title": "Connect an AI tool",
                "client": client.client_name or "An AI tool",
                "returns_to": urlsplit(str(params.redirect_uri)).hostname or "",
                "user": user,
                "request_id": request.query_params.get("request", ""),
            },
        )
        # Never inside a frame: a hidden frame could trick someone into clicking Allow.
        page.headers["X-Frame-Options"] = "DENY"
        page.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        page.headers["Cache-Control"] = "no-store"
        return page

    @app.post(CONSENT_PATH, include_in_schema=False)
    async def decide(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        request_id: Annotated[str, Form()],
        decision: Annotated[str, Form()],
    ) -> Response:
        user = browser_user(request)
        found = await oauth.pending(request_id)
        if found is None:
            return expired(request)
        client, _params = found
        allow = decision == "allow"
        url = await oauth.decide(request_id, user, allow=allow)
        name = client.client_name or "an AI tool"
        return external_redirect(url, f"Continue to {name}")
