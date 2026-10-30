"""Budget module (ADR 0016): a device budget forecast from the fleet, hiring history,
purchase history and a price list. The ``module`` object is this package's entry point."""

from itops_budget import service, tools, views  # registering pages, tools, routes, services
from itops_budget.app import module

__all__ = ["module", "service", "tools", "views"]
