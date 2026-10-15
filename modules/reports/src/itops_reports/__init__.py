"""Reports module (ADR 0016): datasets (from connectors or typed in) and dashboards built on
them. The ``module`` object is this package's entry point."""

from itops_reports import service, tools, views  # registering pages, tools, routes, services, jobs
from itops_reports.app import module

__all__ = ["module", "service", "tools", "views"]
