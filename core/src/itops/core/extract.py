"""Text of uploaded files, page by page (ADR 0015).

Search indexes it (a hit can say "Schedule.pdf, page 2"), and AI suggestions quote it: the
page of a quote is found by searching these pages, never by trusting a model. Pure
functions over bytes; the file store caches the result per file content.

* PDF: each page's text layer (pypdf). A scanned PDF has none: its pages come back empty.
* Word: one page (Word files have no fixed pages): paragraphs, then tables row by row.
* Excel: one page per sheet, cells space-separated per row.
* Outlook .msg: one page: subject, sender, date, then the body.
* Text and CSV: one page.

Anything else, or a file that can't be read, gives no pages. Limits keep a huge file from
filling the index: ``MAX_PAGES`` pages and ``MAX_CHARS`` characters in all.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable
from pathlib import PurePath
from typing import Protocol, cast

MAX_PAGES = 300
MAX_CHARS = 1_000_000
log = logging.getLogger("itops.extract")


def _tidy(text: str) -> str:
    """Collapse runs of spaces and blank lines; keep line breaks (quotes span lines)."""
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _pdf(data: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return [page.extract_text() or "" for page in reader.pages[:MAX_PAGES]]


def _docx(data: bytes) -> list[str]:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return ["\n".join(parts)]


def _xlsx(data: bytes) -> list[str]:
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    pages: list[str] = []
    for sheet in book.worksheets[:MAX_PAGES]:
        rows = ["\t".join("" if v is None else str(v) for v in row)
                for row in sheet.iter_rows(values_only=True)]  # fmt: skip
        pages.append(f"{sheet.title}\n" + "\n".join(r for r in rows if r.strip()))
    return pages


class _Stream(Protocol):
    def read(self) -> bytes: ...


class _Ole(Protocol):
    """The part of olefile.OleFileIO used here (olefile has no type hints)."""

    def exists(self, name: str) -> bool: ...
    def openstream(self, name: str) -> _Stream: ...
    def close(self) -> None: ...


def _msg_stream(ole: _Ole, prop: str) -> str:
    """A MAPI string property: Unicode (001F) or 8-bit (001E)."""
    for suffix, encoding in (("001F", "utf-16-le"), ("001E", "cp1252")):
        name = f"__substg1.0_{prop}{suffix}"
        if ole.exists(name):
            return ole.openstream(name).read().decode(encoding, errors="replace").rstrip("\x00")
    return ""


def _msg(data: bytes) -> list[str]:
    import olefile  # pyright: ignore[reportMissingTypeStubs]

    ole = cast(_Ole, olefile.OleFileIO(io.BytesIO(data)))  # pyright: ignore[reportUnknownMemberType]
    try:
        subject = _msg_stream(ole, "0037")
        sender = _msg_stream(ole, "0C1A") or _msg_stream(ole, "0065")
        headers = _msg_stream(ole, "007D")
        body = _msg_stream(ole, "1000")
    finally:
        ole.close()
    date = next((line[5:].strip() for line in headers.splitlines()
                 if line.lower().startswith("date:")), "")  # fmt: skip
    head = [f"Subject: {subject}" if subject else "", f"From: {sender}" if sender else "",
            f"Date: {date}" if date else ""]  # fmt: skip
    return ["\n".join([*(h for h in head if h), "", body])]


def _text(data: bytes) -> list[str]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return [data.decode(encoding)]
        except UnicodeDecodeError:
            continue
    return []


READERS: dict[str, Callable[[bytes], list[str]]] = {
    ".pdf": _pdf,
    ".docx": _docx,
    ".xlsx": _xlsx,
    ".xlsm": _xlsx,
    ".msg": _msg,
    ".txt": _text,
    ".csv": _text,
    ".log": _text,
}


def readable(filename: str) -> bool:
    return PurePath(filename).suffix.lower() in READERS


def pages(data: bytes, filename: str) -> list[str]:
    """The file's text, one entry per page (sheet for Excel). ``[]`` when it has none or
    can't be read; never raises."""
    reader = READERS.get(PurePath(filename).suffix.lower())
    if reader is None or not data:
        return []
    try:
        raw = reader(data)
    except Exception:  # a damaged or encrypted file: no text, not a failed upload
        log.warning("couldn't read the text of %s", filename, exc_info=True)
        return []
    out: list[str] = []
    budget = MAX_CHARS
    for page in raw[:MAX_PAGES]:
        text = _tidy(page)[:budget]
        out.append(text)
        budget -= len(text)
        if budget <= 0:
            break
    return out if any(out) else []


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def find_quote(page_texts: list[str], quote: str) -> int | None:
    """The 1-based page a quote is on (whitespace and case don't matter), or ``None`` when
    it isn't in the file: then whatever cited it is made up."""
    needle = _squash(quote)
    if len(needle) < 3:
        return None
    for i, text in enumerate(page_texts, start=1):
        if needle in _squash(text):
            return i
    return None
