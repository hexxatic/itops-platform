"""Assets module (ADR 0009): IT inventory with admin-defined types. The ``module`` object
is this package's entry point."""

from itops_assets import service, tools, views  # registering pages, tools, routes, services
from itops_assets.app import module

__all__ = ["module", "service", "tools", "views"]
