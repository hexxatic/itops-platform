"""Text of uploaded files by page (ADR 0015), and finding a quote's page."""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from openpyxl import Workbook

from itops.core import extract


def pdf(*pages: str) -> bytes:
    """A minimal PDF with one line of text per page (Helvetica, uncompressed)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids: list[str] = []
    for text in pages:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        stream_no = len(objects) + 2
        font = len(pages) * 2 + 3
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {stream_no} 0 R"
            f" /Resources << /Font << /F1 {font} 0 R >> >> >>"
        )
        kids.append(f"{len(objects)} 0 R")
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content.decode()}\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def test_pdf_pages_keep_their_numbers() -> None:
    pages = extract.pages(pdf("Schedule of Products", "Expiry Date 05/16/2027"), "s.pdf")
    assert len(pages) == 2 and "Expiry Date 05/16/2027" in pages[1]
    assert extract.find_quote(pages, "expiry   date 05/16/2027") == 2  # spacing, case
    assert extract.find_quote(pages, "Start Date 04/01/2026") is None  # not in the file


def test_word_and_excel(tmp_path: Path) -> None:
    doc = Document()
    doc.add_paragraph("Either party may terminate with ninety (90) days written notice.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Seats", "525"
    buf = io.BytesIO()
    doc.save(buf)
    word = extract.pages(buf.getvalue(), "contract.docx")
    assert len(word) == 1 and "ninety (90) days" in word[0] and "Seats 525" in word[0]

    book = Workbook()
    book.active.title = "Licenses"  # pyright: ignore[reportOptionalMemberAccess]
    book.active.append(["Product", "Qty"])  # pyright: ignore[reportOptionalMemberAccess]
    book.active.append(["MDR Complete", 525])  # pyright: ignore[reportOptionalMemberAccess]
    book.create_sheet("Prices").append(["Total", 14250])
    buf = io.BytesIO()
    book.save(buf)
    sheets = extract.pages(buf.getvalue(), "quote.xlsx")
    assert [s.splitlines()[0] for s in sheets] == ["Licenses", "Prices"]
    assert "MDR Complete 525" in sheets[0]


class FakeOle:
    def __init__(self, streams: dict[str, bytes]) -> None:
        self.streams = streams

    def exists(self, name: str) -> bool:
        return name in self.streams

    def openstream(self, name: str) -> io.BytesIO:
        return io.BytesIO(self.streams[name])

    def close(self) -> None:
        pass


def test_outlook_properties_are_read_in_either_encoding() -> None:
    ole = FakeOle({"__substg1.0_0037001F": "Renewal quote".encode("utf-16-le"),
                   "__substg1.0_1000001E": b"Renews 03/21/2026\x00"})  # fmt: skip
    assert extract._msg_stream(ole, "0037") == "Renewal quote"  # pyright: ignore[reportPrivateUsage]
    assert extract._msg_stream(ole, "1000") == "Renews 03/21/2026"  # pyright: ignore[reportPrivateUsage]
    assert extract._msg_stream(ole, "0C1A") == ""  # pyright: ignore[reportPrivateUsage]


def test_unreadable_or_unknown_files_have_no_text() -> None:
    assert extract.pages(b"not a pdf", "broken.pdf") == []
    assert extract.pages(b"\x89PNG", "shot.png") == []
    assert extract.pages("Ä café".encode(), "notes.txt") == ["Ä café"]
    assert not extract.readable("drawing.vsdx")
