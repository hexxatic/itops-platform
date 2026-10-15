"""News module: updates to the app and announcements from the IT department. The
``module`` object is this package's entry point."""

from itops_news import tools, views  # registering pages, routes and tools
from itops_news.app import module

__all__ = ["module", "tools", "views"]
