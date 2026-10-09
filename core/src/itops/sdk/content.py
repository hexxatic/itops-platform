"""Rich content for modules: the shared block format, sanitizing, Contents, search passages.

    from itops.sdk.content import Content, from_markdown, normalize, toc, passages

Store ``Content`` as JSON (``content.model_dump_json()``); always ``normalize`` before
saving. Display it with the ``ui.content(...)`` component. The section editor produces
it directly.
"""

from itops.core.content import (
    Block,
    BlockView,
    Content,
    HeadingBlock,
    HtmlBlock,
    Passage,
    StepBlock,
    TextBlock,
    TocEntry,
    from_html,
    from_markdown,
    html_to_text,
    normalize,
    passages,
    plain_text,
    render,
    toc,
)

__all__ = [
    "Block",
    "BlockView",
    "Content",
    "HeadingBlock",
    "HtmlBlock",
    "Passage",
    "StepBlock",
    "TextBlock",
    "TocEntry",
    "from_html",
    "from_markdown",
    "html_to_text",
    "normalize",
    "passages",
    "plain_text",
    "render",
    "toc",
]
