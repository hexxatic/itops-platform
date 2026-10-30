"""AI suggestions from a file (ADR 0015): the embedded LLM reads a file and suggests field
values, each with the quote it came from; a person reviews and accepts them.

A module starts a run (``module.ai.suggest(...)``) for one file and the fields it wants;
runs execute one at a time in the background (a CPU model serves one request at a time) and
are kept in ``itops.ai_runs``. Every suggestion is **checked against the file** before
anyone sees it:

* its quote must be in the file (``extract.find_quote``), which also gives its page;
* its value must be in its quote: a date that appears in it (any common format), a number
  that appears in it, text it contains, or one of a select's options;
* anything else is dropped (counted, never shown).

Suggestions never become data on their own: the module shows them for review and records
what a person accepted (``reviewed``). A field marked ``prefill=False`` (e.g. a notice
period, which models confuse with other day counts) is shown with its quote but left for
the person to type.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core import auth, extract
from itops.core.ai import LLM, AIService, AIUnavailable
from itops.core.files import FileStore

log = logging.getLogger("itops.suggest")
FieldKind = Literal["date", "number", "text", "bool", "select"]
MAX_PROMPT_CHARS = 60_000  # ~15k tokens of document; the server's context holds 32k


@dataclass(frozen=True)
class SuggestField:
    """A value a module wants suggested."""

    key: str
    label: str
    kind: FieldKind
    help: str = ""  # what it means, for the model ("days before the end notice must be given")
    options: tuple[str, ...] = ()  # select only
    prefill: bool = True  # False: show the quote, a person types the value


@dataclass(frozen=True)
class Suggestion:
    key: str
    value: Any
    quote: str
    page: int


@dataclass
class SuggestionRun:
    id: int
    module: str
    feature: str
    entity_type: str
    entity_id: str
    file_id: str
    filename: str
    status: str  # queued | running | done | failed | reviewed
    fields: list[SuggestField]
    suggestions: list[Suggestion] = field(default_factory=list[Suggestion])
    dropped: int = 0  # suggestions that failed the checks
    error: str = ""
    model: str = ""
    created_by: str = ""
    created_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed", "reviewed")


# ---- checking a value against its quote -----------------------------------------------------

_MONTHS = {
    name.lower(): i
    for names in (calendar.month_name, calendar.month_abbr)
    for i, name in enumerate(names)
    if name
}
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"), ("m", "d", "y")),
    (re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"), ("m", "d", "y")),
    (re.compile(r"\b([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b"), ("M", "d", "y")),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)? ([A-Za-z]{3,9})\.?,? (\d{4})\b"), ("d", "M", "y")),
]


def dates_in(quote: str) -> set[dt.date]:
    """Every date written in ``quote`` (2027-05-16, 05/16/2027, 5/16/27, May 16, 2027, ...)."""
    found: set[dt.date] = set()
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(quote):
            parts = dict(zip(order, m.groups(), strict=True))
            month = _MONTHS.get(parts["M"].lower()) if "M" in parts else int(parts["m"])
            year = int(parts["y"]) + (2000 if len(parts["y"]) == 2 else 0)
            try:
                if month:
                    found.add(dt.date(year, month, int(parts["d"])))
            except ValueError:
                continue
    return found


def numbers_in(quote: str) -> set[float]:
    return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", quote)}


def checked(f: SuggestField, value: Any, quote: str) -> Any | None:
    """``value`` normalized if ``quote`` backs it up, else ``None``."""
    if value is None or value == "":
        return None
    if f.kind == "date":
        try:
            day = dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
        return day.isoformat() if day in dates_in(quote) else None
    if f.kind == "number":
        try:
            n = float(str(value).replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
        return (int(n) if n.is_integer() else n) if n in numbers_in(quote) else None
    if f.kind == "bool":
        return value if isinstance(value, bool) else None
    if f.kind == "select":
        match = next((o for o in f.options if o.lower() == str(value).strip().lower()), None)
        return match
    s = " ".join(str(value).split())
    return s if s and s.lower() in " ".join(quote.split()).lower() else None


def _prompt(pages: list[str], fields: list[SuggestField], instructions: str) -> str:
    lines: list[str] = []
    for f in fields:
        kind = {
            "date": "a date as YYYY-MM-DD",
            "number": "a number",
            "bool": "true or false",
            "text": "text",
            "select": "one of: " + ", ".join(f.options),
        }[f.kind]
        lines.append(f'- "{f.key}": {f.label} ({kind}){". " + f.help if f.help else ""}')
    doc: list[str] = []
    budget = MAX_PROMPT_CHARS
    for n, page in enumerate(pages, start=1):
        if budget <= 0:
            break
        doc.append(f"[Page {n}]\n{page[:budget]}")
        budget -= len(page)
    return (
        (instructions + "\n\n" if instructions else "")
        + "Find these values in the document below:\n"
        + "\n".join(lines)
        + "\n\n"
        "Answer with ONLY a JSON object: for each key, either null (the document doesn't "
        'say it) or {"value": ..., "quote": "<the exact words from the document it comes '
        'from, copied character for character, at most 40 words>"}. Never guess or '
        "calculate a value the document doesn't state.\n\n--- DOCUMENT ---\n" + "\n\n".join(doc)
    )


async def extract_suggestions(
    llm: LLM, pages: list[str], fields: list[SuggestField], instructions: str = ""
) -> tuple[list[Suggestion], int]:
    """Ask ``llm`` for ``fields`` in a document; return (checked suggestions, dropped count)."""
    if not any(p.strip() for p in pages):
        return [], 0
    answer = await llm.complete(
        _prompt(pages, fields, instructions),
        json=True,
        max_tokens=1500,
        system="You read IT contracts, quotes, invoices and vendor emails and extract facts "
        "exactly as written. /no_think",
    )
    try:
        data: dict[str, Any] = json.loads(answer[answer.find("{") : answer.rfind("}") + 1])
    except ValueError:
        log.warning("the LLM's answer wasn't JSON: %.200s", answer)
        return [], 0
    out: list[Suggestion] = []
    dropped = 0
    for f in fields:
        item = data.get(f.key)
        if not isinstance(item, dict):
            continue
        raw: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
        quote = " ".join(str(raw.get("quote") or "").split())
        page = extract.find_quote(pages, quote)
        value = checked(f, raw.get("value"), quote) if page else None
        if page is None or value is None:
            dropped += 1
            continue
        out.append(Suggestion(key=f.key, value=value, quote=quote, page=page))
    return out, dropped


# ---- runs ----------------------------------------------------------------------------------


def _field(f: dict[str, Any]) -> SuggestField:
    return SuggestField(
        key=str(f["key"]),
        label=str(f["label"]),
        kind=f["kind"],
        help=str(f.get("help", "")),
        options=tuple(str(o) for o in f.get("options", ())),
        prefill=bool(f.get("prefill", True)),
    )


def _run(row: Any) -> SuggestionRun:
    fields = [_field(f) for f in json.loads(row.fields)]
    found: list[dict[str, Any]] = json.loads(row.suggestions) if row.suggestions else []
    return SuggestionRun(
        id=int(row.id),
        module=row.module,
        feature=row.feature,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        file_id=str(row.file_id).lower(),
        filename=row.filename,
        status=row.status,
        fields=fields,
        suggestions=[Suggestion(**s) for s in found],
        dropped=int(row.dropped or 0),
        error=row.error or "",
        model=row.model or "",
        created_by=row.created_by,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


_COLUMNS = (
    "id, module, feature, entity_type, entity_id, file_id, filename, status, fields,"
    " suggestions, dropped, error, model, created_by, created_at, finished_at"
)


@dataclass
class SuggestionRunner:
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    files: FileStore | None = None
    ai: AIService | None = None
    _queue: asyncio.Queue[int] | None = None
    _worker: asyncio.Task[None] | None = None

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        if self.sessionmaker is None:
            raise RuntimeError("suggestions have no database")
        return self.sessionmaker

    async def start(
        self,
        *,
        module: str,
        feature: str,
        file_id: str,
        entity_type: str,
        entity_id: str,
        fields: list[SuggestField],
        instructions: str = "",
    ) -> int:
        """Queue a run; returns its id. The file must be readable by the current principal
        (the module checked that it's this entity's)."""
        assert self.ai is not None and self.files is not None
        if self.ai.llm(f"{module}.{feature}") is None:
            raise AIUnavailable(f"AI suggestions aren't enabled for {module}.{feature}.")
        who = auth.current()
        async with self._maker()() as s:
            row = await self.files.get(s, file_id)
            if row is None:
                raise AIUnavailable("That file doesn't exist.")
            run_id = int(
                (
                    await s.execute(
                        text(
                            "INSERT INTO itops.ai_runs (module, feature, entity_type, entity_id,"
                            " file_id, filename, status, fields, instructions, created_by)"
                            " OUTPUT inserted.id VALUES (:m, :f, :et, :ei, :fid, :fn, 'queued',"
                            " :fields, :ins, :by)"
                        ),
                        {
                            "m": module,
                            "f": feature,
                            "et": entity_type,
                            "ei": entity_id,
                            "fid": file_id,
                            "fn": row.filename,
                            "fields": json.dumps([asdict(f) for f in fields]),
                            "ins": instructions[:4000],
                            "by": who.user if who else "system",
                        },
                    )
                ).scalar_one()
            )
            await s.commit()
        if self._queue is None:
            self._queue = asyncio.Queue()
        await self._queue.put(run_id)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        return run_id

    async def _work(self) -> None:
        assert self._queue is not None
        while not self._queue.empty():
            run_id = await self._queue.get()
            try:
                await self._execute(run_id)
            except Exception:
                log.exception("suggestion run %s failed", run_id)

    async def _execute(self, run_id: int) -> None:
        assert self.ai is not None and self.files is not None
        async with self._maker()() as s:
            row = (
                await s.execute(
                    text(f"SELECT {_COLUMNS}, instructions FROM itops.ai_runs WHERE id = :i"),
                    {"i": run_id},
                )
            ).first()
            if row is None or row.status != "queued":
                return
            await s.execute(
                text(
                    "UPDATE itops.ai_runs SET status = 'running',"
                    " started_at = sysutcdatetime() WHERE id = :i"
                ),
                {"i": run_id},
            )
            await s.commit()
        run = _run(row)
        status, error, found, dropped, model = "done", "", [], 0, ""
        try:
            llm = self.ai.llm(f"{run.module}.{run.feature}")
            if llm is None:
                raise AIUnavailable("AI suggestions were switched off.")
            async with self._maker()() as s:
                file_row = await self.files.get(s, run.file_id)
                pages = await self.files.text(s, file_row) if file_row else []
                await s.commit()
            if not any(p.strip() for p in pages):
                raise AIUnavailable("No text in this file (a scan or an image?): nothing to read.")
            found, dropped = await extract_suggestions(llm, pages, run.fields, row.instructions)
            model = llm.name
        except Exception as exc:
            status, error = "failed", str(exc)[:1000]
            log.warning("suggestion run %s failed: %s", run_id, exc)
        async with self._maker()() as s:
            await s.execute(
                text(
                    "UPDATE itops.ai_runs SET status = :st, suggestions = :sg, dropped = :d,"
                    " error = :e, model = :mo, finished_at = sysutcdatetime() WHERE id = :i"
                ),
                {
                    "st": status,
                    "sg": json.dumps([asdict(x) for x in found]),
                    "d": dropped,
                    "e": error or None,
                    "mo": model or None,
                    "i": run_id,
                },
            )
            await s.commit()

    async def get(self, run_id: int) -> SuggestionRun | None:
        async with self._maker()() as s:
            row = (
                await s.execute(
                    text(f"SELECT {_COLUMNS} FROM itops.ai_runs WHERE id = :i"), {"i": run_id}
                )
            ).first()
        return _run(row) if row else None

    async def for_entity(
        self, module: str, entity_type: str, entity_id: str
    ) -> list[SuggestionRun]:
        """An entity's runs, newest first."""
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    f"SELECT TOP 20 {_COLUMNS} FROM itops.ai_runs WHERE module = :m"
                    " AND entity_type = :t AND entity_id = :e ORDER BY id DESC"
                ),
                {"m": module, "t": entity_type, "e": entity_id},
            )
            return [_run(r) for r in rows.all()]

    async def to_review(self, module: str, user: str) -> list[SuggestionRun]:
        """``user``'s finished runs nobody has reviewed yet, newest first."""
        async with self._maker()() as s:
            rows = await s.execute(
                text(
                    f"SELECT TOP 50 {_COLUMNS} FROM itops.ai_runs WHERE module = :m"
                    " AND created_by = :u AND status = 'done' ORDER BY id DESC"
                ),
                {"m": module, "u": user},
            )
            return [_run(r) for r in rows.all()]

    async def reviewed(self, run_id: int, *, accepted: dict[str, Any], by: str) -> None:
        """Record what a person accepted (key -> the value saved, which may differ)."""
        async with self._maker()() as s:
            await s.execute(
                text(
                    "UPDATE itops.ai_runs SET status = 'reviewed', accepted = :a,"
                    " reviewed_by = :by, reviewed_at = sysutcdatetime() WHERE id = :i"
                ),
                {"a": json.dumps(accepted, default=str), "by": by, "i": run_id},
            )
            await s.commit()

    async def recover(self) -> int:
        """Runs a stopped service left queued or running are marked failed."""
        async with self._maker()() as s:
            result = await s.execute(
                text(
                    "UPDATE itops.ai_runs SET status = 'failed', finished_at = sysutcdatetime(),"
                    " error = 'Interrupted: the service stopped. Start it again.'"
                    " WHERE status IN ('queued', 'running')"
                )
            )
            await s.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def wait(self) -> None:
        """Until the queue is empty (tests)."""
        if self._worker is not None:
            await self._worker
