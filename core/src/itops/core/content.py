"""Rich content: the platform's shared content format (ADR 0003).

Content is a list of typed **blocks**, stored as structured data:

* ``heading``: a section title (level 2 or 3). Builds the Contents list.
* ``text``: rich text (paragraphs, lists, links, images, code, tables).
* ``step``: rich text shown as a numbered procedure step, with an optional duration.
* ``html``: rich HTML kept exactly as authored (e.g. a Claude-generated runbook),
  sanitized, and displayed in a locked-down frame.

Every HTML fragment is sanitized on the way in (:func:`normalize`), so what is stored
is already browser-safe. Images may only point at the platform's own file store
(``/files/...``): no external tracking pixels, no data exfiltration via image URLs.
"""

from __future__ import annotations

import html as htmllib
import re
from html.parser import HTMLParser
from typing import Annotated, Literal

import nh3
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

# ---- the format ---------------------------------------------------------------------


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeadingBlock(_Block):
    type: Literal["heading"] = "heading"
    level: Literal[2, 3] = 2
    text: str


class TextBlock(_Block):
    type: Literal["text"] = "text"
    html: str


class StepBlock(_Block):
    type: Literal["step"] = "step"
    html: str
    duration_min: int | None = Field(default=None, ge=0, le=100_000)
    title: str = Field(default="", max_length=300)  # optional; shown and listed in Contents


class HtmlBlock(_Block):
    type: Literal["html"] = "html"
    html: str


Block = Annotated[HeadingBlock | TextBlock | StepBlock | HtmlBlock, Field(discriminator="type")]


class Content(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[Block] = Field(default_factory=list[Block])


# ---- sanitizing ---------------------------------------------------------------------

FILES_PREFIX = "/files/"

# Text and step bodies: the inline + structural set a rich-text box produces.
_TEXT_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "code", "pre", "blockquote",
    "ul", "ol", "li", "a", "img", "h4", "h5", "h6", "hr", "span", "div",
    "table", "thead", "tbody", "tr", "th", "td", "sup", "sub", "kbd", "mark",
}  # fmt: skip
# HTML blocks: the rich structures AI-generated documentation uses.
_HTML_TAGS = _TEXT_TAGS | {
    "h2", "h3", "section", "article", "header", "footer", "details", "summary",
    "figure", "figcaption", "caption", "colgroup", "col", "tfoot", "dl", "dt", "dd",
    "abbr", "small", "time",
}  # fmt: skip
_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"class", "title"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
    "ol": {"start"},
    "details": {"open"},
    "time": {"datetime"},
}
_URL_SCHEMES = {"http", "https", "mailto"}


def _attribute_filter(tag: str, attr: str, value: str) -> str | None:
    if tag == "img" and attr == "src" and not value.startswith(FILES_PREFIX):
        return None  # only the platform's own file store
    return value


def _clean(fragment: str, tags: set[str]) -> str:
    return nh3.clean(
        fragment,
        tags=tags,
        attributes=_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    ).strip()


def sanitize_text(fragment: str) -> str:
    """Sanitize a text/step body."""
    return _clean(fragment, _TEXT_TAGS)


def sanitize_rich(fragment: str) -> str:
    """Sanitize an HTML block (a wider allowlist, still no script/style/iframe/forms)."""
    return _clean(fragment, _HTML_TAGS)


_VISIBLE_RE = re.compile(r"<(?:img|hr|table)[\s/>]", re.IGNORECASE)


def _blank(fragment: str) -> bool:
    """No visible content: no text, and no image/rule/table (a lone screenshot counts)."""
    return not html_to_text(fragment) and not _VISIBLE_RE.search(fragment)


_VOID = {"br", "img", "hr"}
_BLOCKS = {"p", "ul", "ol", "table", "pre", "blockquote", "h4", "h5", "h6", "hr"}
_UNWRAP = {"div", "p"}  # their children are re-flowed into proper paragraphs


class _Node:
    def __init__(self, tag: str | None, attrs: list[tuple[str, str | None]], text: str = ""):
        self.tag = tag  # None = text node
        self.attrs = attrs
        self.text = text
        self.children: list[_Node] = []


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(_Node(None, [], data))


def _serialize(node: _Node) -> str:
    if node.tag is None:
        return htmllib.escape(node.text, quote=False)
    attrs = "".join(f' {k}="{htmllib.escape(v or "", quote=True)}"' for k, v in node.attrs)
    if node.tag in _VOID:
        return f"<{node.tag}{attrs}>"
    inner = "".join(_serialize(c) for c in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def _meaningful(nodes: list[_Node]) -> bool:
    for n in nodes:
        if n.tag is None and n.text.strip():
            return True
        if n.tag in ("img", "hr"):
            return True
        if n.tag not in (None, "br") and _meaningful(n.children):
            return True
    return False


def tidy_text(fragment: str) -> str:
    """Re-flow a text/step body into clean paragraphs.

    Rich-text boxes in browsers emit bare text lines, ``<div>`` wrappers and trailing
    ``<div><br></div>`` blanks. This turns every run of inline content into one ``<p>``
    (a ``<div>`` or ``<p>`` boundary starts a new one), keeps lists/tables/code as they
    are, and drops empty paragraphs. Input must already be sanitized.
    """
    builder = _TreeBuilder()
    builder.feed(fragment)
    builder.close()
    out: list[str] = []
    inline: list[_Node] = []

    def flush() -> None:
        # Trim leading/trailing line breaks, then keep the paragraph only if it has content.
        while inline and inline[0].tag == "br":
            inline.pop(0)
        while inline and inline[-1].tag == "br":
            inline.pop()
        if _meaningful(inline):
            out.append("<p>" + "".join(_serialize(n) for n in inline).strip() + "</p>")
        inline.clear()

    def walk(nodes: list[_Node]) -> None:
        for n in nodes:
            if n.tag in _UNWRAP:
                flush()
                walk(n.children)
                flush()
            elif n.tag in _BLOCKS:
                flush()
                if n.tag == "hr" or _meaningful(n.children):
                    out.append(_serialize(n))
            else:
                inline.append(n)

    walk(builder.root.children)
    flush()
    return "".join(out)


def normalize(content: Content) -> Content:
    """Sanitize every block and drop empty ones. Run on every save."""
    out: list[Block] = []
    for b in content.blocks:
        match b:
            case HeadingBlock():
                text = " ".join(b.text.split())
                if text:
                    out.append(HeadingBlock(level=b.level, text=text))
            case TextBlock():
                html = tidy_text(sanitize_text(b.html))
                if not _blank(html):
                    out.append(TextBlock(html=html))
            case StepBlock():
                html = tidy_text(sanitize_text(b.html))
                title = " ".join(b.title.split())
                if title or not _blank(html):
                    out.append(StepBlock(html=html, duration_min=b.duration_min, title=title))
            case HtmlBlock():
                html = sanitize_rich(b.html)
                if not _blank(html):
                    out.append(HtmlBlock(html=html))
    return Content(blocks=out)


# ---- reading ------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|pre|blockquote|table|thead|tbody|tfoot|tr|td|th|"
    r"section|article|header|footer|details|summary|figure|figcaption|dl|dt|dd|hr|img)\b[^>]*>",
    re.IGNORECASE,
)


def html_to_text(fragment: str) -> str:
    """Plain text from sanitized HTML (search indexing, previews). Not for display.

    Block boundaries become word breaks; inline tags (<code>, <strong>, ...) vanish
    without adding spaces, so "the <code>dryRun</code> text." stays "the dryRun text.".
    """
    text = _TAG_RE.sub("", _BLOCK_TAG_RE.sub(" ", fragment))
    return _WS_RE.sub(" ", htmllib.unescape(text)).strip()


def anchor(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "section"


class TocEntry(BaseModel):
    level: int
    text: str
    anchor: str


def toc(content: Content) -> list[TocEntry]:
    """The Contents list: one entry per heading and per titled step, in order, with
    unique anchors. Step titles sit under the headings (level 3), or at the top level
    when the content has no headings."""
    seen: dict[str, int] = {}
    entries: list[TocEntry] = []
    step_level = 3 if any(isinstance(b, HeadingBlock) for b in content.blocks) else 2
    for b in content.blocks:
        if isinstance(b, HeadingBlock):
            text, level = b.text, b.level
        elif isinstance(b, StepBlock) and b.title:
            text, level = b.title, step_level
        else:
            continue
        base = anchor(text)
        seen[base] = seen.get(base, 0) + 1
        a = base if seen[base] == 1 else f"{base}-{seen[base]}"
        entries.append(TocEntry(level=level, text=text, anchor=a))
    return entries


class BlockView(BaseModel):
    """A block ready for the ``ui.content`` component: anchors and step numbers resolved."""

    type: str
    html: str = ""  # already sanitized on save
    text: str = ""
    level: int = 2
    anchor: str | None = None
    step: int | None = None
    duration_min: int | None = None
    title: str = ""  # a step's title


def render(content: Content) -> list[BlockView]:
    """Blocks for display. Content was sanitized on save; this resolves presentation."""
    anchors = iter(toc(content))
    step = 0
    out: list[BlockView] = []
    for b in content.blocks:
        match b:
            case HeadingBlock():
                out.append(
                    BlockView(
                        type="heading", text=b.text, level=b.level, anchor=next(anchors).anchor
                    )
                )
            case StepBlock():
                step += 1
                out.append(
                    BlockView(
                        type="step", html=tidy_text(b.html), step=step,
                        duration_min=b.duration_min, title=b.title,
                        anchor=next(anchors).anchor if b.title else None,
                    )
                )  # fmt: skip
            case TextBlock():
                # Tidy again on display so content saved before tidy_text existed looks right.
                out.append(BlockView(type="text", html=tidy_text(b.html)))
            case HtmlBlock():
                out.append(BlockView(type="html", html=b.html))
    return out


class Passage(BaseModel):
    """A citable piece of content for the search index ("Failover > Step 3")."""

    label: str
    anchor: str | None
    text: str


def plain_text(content: Content) -> str:
    """Readable, markdown-like text of the whole content (for AI tools and exports):
    ``## Heading`` lines, ``Step N:`` for steps, plain text for the rest."""
    lines: list[str] = []
    step = 0
    for b in content.blocks:
        match b:
            case HeadingBlock():
                lines.append(("## " if b.level == 2 else "### ") + b.text)
            case StepBlock():
                step += 1
                took = f" (about {b.duration_min} min)" if b.duration_min else ""
                title = f"{b.title}. " if b.title else ""
                lines.append(f"Step {step}{took}: {title}{html_to_text(b.html)}")
            case TextBlock() | HtmlBlock():
                lines.append(html_to_text(b.html))
    return "\n\n".join(line for line in lines if line.strip())


def passages(content: Content) -> list[Passage]:
    """Split content into search passages, one per text/step/html block, labelled by the
    heading it sits under ("Failover > Step 3"). Headings themselves aren't passages:
    they're the labels."""
    heading: TocEntry | None = None
    headings = iter(toc(content))
    step = 0
    out: list[Passage] = []
    for b in content.blocks:
        match b:
            case HeadingBlock():
                heading = next(headings)
            case StepBlock():
                step += 1
                where = f"{heading.text} > " if heading else ""
                own = next(headings) if b.title else None  # a titled step has its own anchor
                label = f"{where}Step {step}" + (f": {b.title}" if b.title else "")
                text = " ".join(filter(None, [b.title, html_to_text(b.html)]))
                out.append(
                    Passage(
                        label=label,
                        anchor=own.anchor if own else (heading.anchor if heading else None),
                        text=text,
                    )
                )
            case TextBlock() | HtmlBlock():
                out.append(
                    Passage(
                        label=heading.text if heading else "",
                        anchor=heading.anchor if heading else None,
                        text=html_to_text(b.html),
                    )
                )
    return [p for p in out if p.text]


# ---- importing ----------------------------------------------------------------------

_md = MarkdownIt("commonmark", {"html": False}).enable("table")
_SPLIT_RE = re.compile(r"(<h[23]>.*?</h[23]>)", re.DOTALL)


def from_markdown(markdown: str) -> Content:
    """Markdown (what AI tools and imports often send) -> blocks.

    ``##`` / ``###`` headings become heading blocks; everything between them becomes a
    text block. The result still goes through :func:`normalize` on save.
    """
    rendered = _md.render(markdown)
    blocks: list[Block] = []
    for part in _SPLIT_RE.split(rendered):
        if not part.strip():
            continue
        m = re.fullmatch(r"<h([23])>(.*?)</h[23]>", part.strip(), re.DOTALL)
        if m:
            level: Literal[2, 3] = 2 if m[1] == "2" else 3
            blocks.append(HeadingBlock(level=level, text=html_to_text(m[2])))
        else:
            blocks.append(TextBlock(html=part.strip()))
    return Content(blocks=blocks)


def from_html(fragment: str) -> Content:
    """Rich HTML kept as one HTML block (sanitized on save)."""
    return Content(blocks=[HtmlBlock(html=fragment)])
