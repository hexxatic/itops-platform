"""How-tos (kb): IT's how-tos for staff on the help site (ADR 0027), read from Docs, and what
people think of them. The ``module`` object is this package's entry point."""

from itops_kb import service, tools, views  # registering services, tools, pages
from itops_kb.app import module

__all__ = ["module", "service", "tools", "views"]
