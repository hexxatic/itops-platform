"""Search snippets: HTML for the page, plain text for AI tools and the API."""

from __future__ import annotations

from itops.core.search import Hit, snippet


def test_snippet_is_escaped_html_with_marks() -> None:
    out = snippet("Run 'Restart-Service Spooler' <as admin>", ["spooler"])
    assert out == "Run &#x27;Restart-Service <mark>Spooler</mark>&#x27; &lt;as admin&gt;"


def test_hit_dict_snippet_is_plain_text() -> None:
    html = snippet("Run 'Restart-Service Spooler' <as admin> & wait", ["spooler"])
    hit = Hit("docs", "document", "1", "Printing", "/docs/d/printing", "", html, 1.0)
    assert hit.as_dict()["snippet"] == "Run 'Restart-Service Spooler' <as admin> & wait"


def test_long_passages_are_chunked() -> None:
    from itops.core.content import Passage
    from itops.core.search import CHUNK_CHARS, chunk

    words = " ".join(f"word{i}" for i in range(CHUNK_CHARS // 2))
    pieces = chunk(Passage(label="NCM V1", anchor="design", text=words))
    assert len(pieces) > 1
    assert all(p.label == "NCM V1" and p.anchor == "design" for p in pieces)
    assert all(len(p.text) <= CHUNK_CHARS for p in pieces)
    assert pieces[0].text.startswith("word0 ") and pieces[-1].text.endswith(words[-10:])
