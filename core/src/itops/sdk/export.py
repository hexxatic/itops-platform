"""Lists as CSV or Excel downloads (ADR 0016).

    from itops.sdk.export import Column, ExportProblem, export_response

    @module.router.get("/things/export")
    async def export(format: str = "xlsx", ...) -> Response:
        rows = [...]  # dicts, or ui.table-style cells ({"text": ..., "href": ...})
        cols = [Column("name", "Name"), Column("cost", "Cost", "money"),
                Column("ends", "Ends", "date")]
        return export_response(rows, cols, filename="Things", format=format)

Show the links with ``ui.export_links(url)``. Column kinds: text, number, money, date,
datetime. Excel keeps the types; text never becomes a formula in either format.
``money(v)`` / ``number(v)`` format figures the way the ``|money`` / ``|number`` filters do.
"""

from itops.core.charts import money, number
from itops.core.export import Column, ColumnKind, ExportProblem, export_response

__all__ = ["Column", "ColumnKind", "ExportProblem", "export_response", "money", "number"]
