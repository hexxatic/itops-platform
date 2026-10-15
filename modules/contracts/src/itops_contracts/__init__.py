"""Contracts module (ADR 0014): IT contracts, their terms, renewals, costs and seats. The
``module`` object is this package's entry point."""

from itops_contracts import service, tools, views  # registering pages, tools, routes, jobs
from itops_contracts.app import module

__all__ = ["module", "service", "tools", "views"]
