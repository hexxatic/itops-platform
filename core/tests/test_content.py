"""The shared content format: sanitizing (security), Contents, passages, markdown import."""

from __future__ import annotations

import html as htmllib
import re
from typing import Any

from itops.core.config import Settings
from itops.core.content import (
    Content,
    HeadingBlock,
    HtmlBlock,
    StepBlock,
    TextBlock,
    from_markdown,
    normalize,
    passages,
    render,
    toc,
)
from itops.core.ui import build_templates


def clean(block: TextBlock | StepBlock | HtmlBlock) -> str:
    out = normalize(Content(blocks=[block])).blocks
    return out[0].html if out else ""  # type: ignore[union-attr]


def test_scripts_handlers_and_styles_are_stripped() -> None:
    dirty = (
        '<p onclick="steal()" style="color:red">hi<script>alert(1)</script></p>'
        '<iframe src="https://evil"></iframe><style>*{}</style>'
        '<a href="javascript:alert(1)">x</a><form><input></form>'
    )
    for block in (TextBlock(html=dirty), HtmlBlock(html=dirty)):
        out = clean(block)
        for bad in ("script", "onclick", "style", "iframe", "javascript:", "<form", "<input"):
            assert bad not in out, (bad, out)
        assert "hi" in out


def test_images_only_from_the_platform_file_store() -> None:
    out = clean(TextBlock(html='<img src="/files/abc"><img src="https://tracker/p.gif">'))
    assert 'src="/files/abc"' in out
    assert "tracker" not in out


def test_links_get_safe_rel() -> None:
    out = clean(TextBlock(html='<a href="https://example.com">x</a>'))
    assert 'rel="noopener noreferrer nofollow"' in out


def test_html_blocks_keep_rich_structure() -> None:
    rich = (
        "<details open><summary>More</summary><table><thead><tr><th>A</th></tr></thead>"
        "<tbody><tr><td>1</td></tr></tbody></table></details><h2>Part</h2>"
    )
    out = clean(HtmlBlock(html=rich))
    for kept in ("<details", "<summary>", "<table>", "<th>", "<h2>"):
        assert kept in out
    # ...but text blocks don't allow document-level structure like h2/details.
    assert "<details" not in clean(TextBlock(html=rich))


def test_empty_blocks_are_dropped_and_headings_trimmed() -> None:
    content = normalize(
        Content(
            blocks=[
                HeadingBlock(text="  Setup   steps "),
                TextBlock(html="<p>   </p>"),
                StepBlock(html="<script>x</script>"),
            ]
        )
    )
    assert content.blocks == [HeadingBlock(text="Setup steps")]


def test_toc_anchors_are_unique() -> None:
    c = Content(blocks=[HeadingBlock(text="Notes"), HeadingBlock(level=3, text="Notes")])
    assert [(e.level, e.anchor) for e in toc(c)] == [(2, "notes"), (3, "notes-2")]


def test_passages_are_labelled_for_citations() -> None:
    c = Content(
        blocks=[
            HeadingBlock(text="Failover"),
            StepBlock(html="<p>Stop the primary</p>"),
            StepBlock(html="<p>Promote the replica</p>"),
            TextBlock(html="<p>Done.</p>"),
        ]
    )
    labels = [(p.label, p.anchor) for p in passages(c)]
    assert labels == [  # headings label the passages under them; they aren't passages
        ("Failover > Step 1", "failover"),
        ("Failover > Step 2", "failover"),
        ("Failover", "failover"),
    ]


def test_markdown_splits_on_headings() -> None:
    c = from_markdown("Intro text\n\n## Install\n\nRun **setup**.\n\n### Notes\n\n- a\n- b\n")
    kinds = [b.type for b in c.blocks]
    assert kinds == ["text", "heading", "text", "heading", "text"]
    assert c.blocks[1] == HeadingBlock(level=2, text="Install")
    assert "<strong>setup</strong>" in c.blocks[2].html  # type: ignore[union-attr]


def test_screenshot_only_block_is_kept() -> None:
    assert clean(StepBlock(html='<p><img src="/files/x"></p>'))


def test_tidy_reflows_browser_markup_into_paragraphs() -> None:
    from itops.core.content import tidy_text

    # Exactly what the editor produced for the first real test document.
    raw = 'Badger<div><img src="/files/ef11" alt=""><div><br></div></div>'
    assert tidy_text(raw) == '<p>Badger</p><p><img src="/files/ef11" alt=""></p>'
    assert tidy_text("<div>one</div><div>two</div>") == "<p>one</p><p>two</p>"
    assert tidy_text("<p><br></p><p>kept</p><div><br></div>") == "<p>kept</p>"
    assert tidy_text("line <strong>bold</strong> end") == "<p>line <strong>bold</strong> end</p>"


def test_tidy_keeps_lists_tables_and_escapes_text() -> None:
    from itops.core.content import tidy_text

    assert (
        tidy_text("intro<ul><li>a</li></ul>outro") == "<p>intro</p><ul><li>a</li></ul><p>outro</p>"
    )
    assert tidy_text("<pre>x &lt; y</pre>") == "<pre>x &lt; y</pre>"
    assert tidy_text("a &amp; b") == "<p>a &amp; b</p>"
    assert tidy_text("<ul></ul><p>x</p>") == "<p>x</p>"  # empty list dropped


def test_html_blocks_render_in_a_locked_down_frame() -> None:
    env = build_templates([], Settings()).env
    ui: Any = env.get_template("core/components.html").module
    blocks = render(
        normalize(Content(blocks=[HtmlBlock(html="<h2>Runbook</h2><p>Hi &amp; bye</p>")]))
    )
    out = str(ui.content(blocks))

    frame = re.search(r"<iframe[^>]*>", out)
    assert frame, out
    tag = frame.group(0)
    sandbox = re.search(r'sandbox="([^"]*)"', tag)
    assert sandbox and "allow-scripts" not in sandbox.group(1)
    assert "allow-forms" not in sandbox.group(1)
    srcdoc = htmllib.unescape(re.search(r'srcdoc="([^"]*)"', tag).group(1))  # type: ignore[union-attr]
    assert "default-src 'none'" in srcdoc and "form-action 'none'" in srcdoc
    assert "<h2>Runbook</h2><p>Hi &amp; bye</p>" in srcdoc  # the block, escaped only once
    assert "<h2>Runbook</h2>" not in out.replace(frame.group(0), "")  # never inline in the page


def test_step_titles_are_listed_rendered_and_searchable() -> None:
    content = normalize(Content(blocks=[
        StepBlock(html="<p>Open the BIOS</p>", title="  Disable  serial ports "),
        StepBlock(html="<p>No title here</p>"),
        StepBlock(html="", title="Reboot"),  # a title alone is a step
    ]))  # fmt: skip
    assert [b.title for b in content.blocks if isinstance(b, StepBlock)] == [
        "Disable serial ports", "", "Reboot"]  # fmt: skip
    entries = toc(content)
    assert [(e.level, e.text, e.anchor) for e in entries] == [
        (2, "Disable serial ports", "disable-serial-ports"), (2, "Reboot", "reboot")]  # fmt: skip
    views = render(content)
    assert (views[0].title, views[0].anchor, views[1].anchor) == (
        "Disable serial ports", "disable-serial-ports", None)  # fmt: skip
    found = passages(content)
    assert found[0].label == "Step 1: Disable serial ports"
    assert found[0].anchor == "disable-serial-ports" and found[0].text.startswith("Disable")
    # Under a heading, step titles are its subentries.
    nested = toc(
        Content(blocks=[HeadingBlock(text="Setup"), StepBlock(html="<p>x</p>", title="A")])
    )
    assert [(e.level, e.text) for e in nested] == [(2, "Setup"), (3, "A")]
