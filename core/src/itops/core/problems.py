"""What a connector or tool run tells people: problems to act on, and notes that only inform.

Both are kept in a run's ``problems`` JSON as ``{item, text, url?, note?}``: a note carries
``"note": "1"`` and doesn't make the run "finished with problems". ``url`` is a page in IT-Ops
where the person acts (a relative path; anything else is dropped).
"""

from __future__ import annotations

from typing import Any


def local_url(url: str | None) -> str | None:
    """``url`` if it's a path inside IT-Ops ("/assets/r/..."), else None."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return None
    return url[:500]


class Report:
    """The problems and notes of one run, capped at ``limit`` each; past that, one line says
    how many more there were."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.entries: list[dict[str, str]] = []
        self._left_out = {"problem": 0, "note": 0}
        self._listed = {"problem": 0, "note": 0}

    def add(self, message: str, *, item: str | None, url: str | None, note: bool = False) -> None:
        kind = "note" if note else "problem"
        if self._listed[kind] >= self.limit:
            self._left_out[kind] += 1
            n = self._left_out[kind]
            text = (f"1 more {kind} wasn't listed here." if n == 1
                    else f"{n} more {kind}s weren't listed here.")  # fmt: skip
            last = next((e for e in reversed(self.entries) if e.get("left_out") == kind), None)
            if last is None:
                last = {"item": "", "text": text, "left_out": kind}
                if note:
                    last["note"] = "1"
                self.entries.append(last)
            last["text"] = text
            return
        self._listed[kind] += 1
        entry = {"item": (item or "")[:300], "text": message[:1000]}
        if link := local_url(url):
            entry["url"] = link
        if note:
            entry["note"] = "1"
        self.entries.append(entry)

    @property
    def has_problems(self) -> bool:
        return any(not e.get("note") for e in self.entries)


def split(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(problems, notes) of a run's stored entries."""
    return [e for e in entries if not e.get("note")], [e for e in entries if e.get("note")]


def rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Table rows for a run page: the item, the text, and a link to act on it."""
    return [
        {"item": e.get("item", ""), "text": e.get("text", ""),
         "act": {"text": "Open", "href": e["url"]} if e.get("url") else ""}
        for e in entries
    ]  # fmt: skip
