"""``/apps``: tokens for other internal applications reading IT-Ops as themselves, for
admins (ADR 0021). Issue one (shown once), see when each was last used, revoke it."""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.apps import MAX_APP_TOKEN_DAYS, AppTokenError, AppTokens
from itops.core.audit import AUDIT_PERMISSION

APPS_PERMISSION = AUDIT_PERMISSION  # who may let another system in: admins
PERM_FIELD = "perm__"


def _when(value: dt.datetime | None, never: str) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if value else never


def mount_apps(app: FastAPI, templates: Jinja2Templates, apps: AppTokens) -> None:
    def admin() -> None:
        """Admins, in a browser: no token (AI or app) may let another system in."""
        try:
            principal = auth.require(APPS_PERMISSION)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail="This page is for admins.") from exc
        if principal.via_ai or principal.client != "web":
            raise HTTPException(status_code=403, detail="Open this page in a browser.")

    async def render(
        request: Request,
        *,
        new_token: str | None = None,
        error: str | None = None,
        slug: str = "",
        name: str = "",
        chosen: Collection[str] = (),
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "core/apps.html",
            {
                "page_title": "Apps",
                "current_module": "admin",
                "new_token": new_token,
                "error": error,
                "slug": slug,
                "name": name,
                "permission_choices": [
                    {"field": PERM_FIELD + p, "label": p, "checked": p in chosen}
                    for p in apps.choices()
                ],
                "day_options": [
                    (30, "30 days"),
                    (90, "90 days"),
                    (180, "180 days"),
                    (MAX_APP_TOKEN_DAYS, "1 year"),
                ],
                "max_days": MAX_APP_TOKEN_DAYS,
                "token_rows": [
                    {
                        "app": t.slug,
                        "name": t.name,
                        "prefix": t.prefix + "…",
                        "permissions": ", ".join(t.permissions),
                        "expires": f"{t.expires_at:%Y-%m-%d}",
                        "used": _when(t.last_used_at, "Never"),
                        "revoke": {
                            "action": f"/apps/tokens/{t.id}/revoke",
                            "label": "Revoke",
                            "variant": "danger",
                            "confirm": f"Revoke this token for “{t.name}”? The app stops"
                            " reading IT-Ops until it gets a new one.",
                        },
                    }
                    for t in await apps.live()
                ],
            },
        )

    @app.get("/apps", response_class=HTMLResponse, include_in_schema=False)
    async def page(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        admin()
        return await render(request)

    @app.post("/apps/tokens", response_class=HTMLResponse, include_in_schema=False)
    async def create(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        admin()
        data = await request.form()
        slug, name = str(data.get("slug", "")), str(data.get("name", ""))
        chosen = [k[len(PERM_FIELD) :] for k in data if k.startswith(PERM_FIELD)]
        try:
            days = int(str(data.get("days", MAX_APP_TOKEN_DAYS)))
        except ValueError:
            days = MAX_APP_TOKEN_DAYS
        try:
            token = await apps.create(slug, name, chosen, days)
        except AppTokenError as exc:
            return await render(request, error=str(exc), slug=slug, name=name, chosen=chosen)
        # Shown once, in this response only; never stored or logged.
        return await render(request, new_token=token)

    @app.post("/apps/tokens/{token_id}/revoke", include_in_schema=False)
    async def revoke(token_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        admin()
        await apps.revoke(token_id)
        return RedirectResponse("/apps", status_code=303)
