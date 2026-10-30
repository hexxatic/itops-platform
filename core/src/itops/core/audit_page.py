"""``/audit``: the whole audit trail, newest first, for admins in a browser (ADR 0006)."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from itops.core import auth
from itops.core.audit import AUDIT_PERMISSION
from itops.core.runtime import Runtime

PAGE_SIZE = 100


def mount_audit(app: FastAPI, templates: Jinja2Templates, runtime: Runtime) -> None:
    @app.get("/audit", response_class=HTMLResponse, include_in_schema=False)
    async def audit_page(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        module: str = "",
        who: str = "",
        ai: bool = False,
        before: int | None = None,
    ) -> Response:
        try:
            principal = auth.require(AUDIT_PERMISSION)
        except auth.PermissionDenied as exc:
            raise HTTPException(status_code=403, detail="The audit trail is for admins.") from exc
        # Admins, in a browser. Entries carry titles and summaries from areas kept out of AI
        # (ADR 0012) and don't say which area they belong to, so a token (an AI tool) gets
        # none of them, like /ai-connections and /access.
        if principal.via_ai:
            raise HTTPException(status_code=403, detail="Open this page in a browser.")
        maker = runtime.sessionmaker
        assert maker is not None
        async with maker() as s:
            entries = await runtime.audit.query(
                s,
                module=module or None,
                actor=who.strip() or None,
                via_ai=ai,
                before_id=before,
                limit=PAGE_SIZE,
            )
            modules = await runtime.audit.modules(s)
        older = None
        if len(entries) == PAGE_SIZE:
            params = {"module": module, "who": who, "before": entries[-1].id}
            if ai:
                params["ai"] = 1
            older = "/audit?" + urlencode({k: v for k, v in params.items() if v})
        return templates.TemplateResponse(
            request,
            "core/audit.html",
            {
                "page_title": "Audit trail",
                "current_module": "admin",
                "entries": entries,
                "modules": [("", "All modules")] + [(m, m) for m in modules],
                "module": module,
                "who": who,
                "ai": ai,
                "older": older,
            },
        )
