"""Reports module (ADR 0016): datasets (from connectors or typed in) and dashboards built on
them. The ``module`` object is this package's entry point."""

from itops_reports import aggregate, service, tools, views  # registering services and pages
from itops_reports.app import module

__all__ = ["aggregate", "module", "service", "tools", "views"]
