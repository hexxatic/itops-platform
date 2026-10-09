"""Docs module: IT documentation with section-based documents, drafts, publishing and
version history. The ``module`` object is this package's entry point."""

from itops_docs import retire, sync, tools, views  # registering pages, routes, tools, services
from itops_docs.app import module

__all__ = ["module", "retire", "sync", "tools", "views"]
