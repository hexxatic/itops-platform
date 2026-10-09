"""Form input kept across the redirect after a failed save.

Pages post to an action that redirects back (post/redirect/get). When the save fails,
the action keeps what was submitted here and adds ``kept=<token>`` to the redirect, so the
page can fill the form in again instead of making people retype it.

Kept forms live in memory for a short while, belong to the user who submitted them, and
are size-capped. Losing one (restart, expiry) just means an empty form, as before.
Uploaded files are never kept; ``files_dropped`` says some were chosen.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

TTL_SECONDS = 15 * 60
MAX_FORMS = 500
MAX_CHARS = 4_000_000  # per form; a long document's content fits comfortably


@dataclass(frozen=True)
class KeptForm:
    """A submitted form: every name with its values (repeated names = several values)."""

    values: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    files_dropped: bool = False

    def get(self, name: str, default: str = "") -> str:
        found = self.values.get(name)
        return found[0] if found else default

    def getlist(self, name: str) -> list[str]:
        return list(self.values.get(name, []))

    def __contains__(self, name: object) -> bool:
        return name in self.values

    def fields(self, prefix: str = "f__") -> dict[str, Any]:
        """Record-type field values (inputs named ``<prefix><key>``), shaped like a save."""
        out: dict[str, Any] = {}
        for name, raw in self.values.items():
            if name.startswith(prefix):
                values = [v for v in raw if v.strip()]
                out[name[len(prefix) :]] = (
                    values if len(values) > 1 else (values[0] if values else "")
                )
        return out


class FormKeeper:
    def __init__(self) -> None:
        self._forms: OrderedDict[str, tuple[float, str, KeptForm]] = OrderedDict()

    def keep(self, user: str, form: KeptForm) -> str | None:
        """Store ``form`` for ``user``; returns the token, or None if it's too big."""
        if sum(len(k) + sum(len(v) for v in vs) for k, vs in form.values.items()) > MAX_CHARS:
            return None
        self._expire()
        token = secrets.token_urlsafe(16)
        self._forms[token] = (time.monotonic() + TTL_SECONDS, user, form)
        while len(self._forms) > MAX_FORMS:
            self._forms.popitem(last=False)
        return token

    def recall(self, user: str, token: str | None) -> KeptForm | None:
        """The form kept under ``token``, only for the user who submitted it. Not removed:
        reloading the page still shows it until it expires."""
        if not token:
            return None
        self._expire()
        found = self._forms.get(token)
        if found is None or not secrets.compare_digest(found[1], user):
            return None
        return found[2]

    def _expire(self) -> None:
        now = time.monotonic()
        for token in [t for t, (until, _, _) in self._forms.items() if until < now]:
            del self._forms[token]
