"""HTML attachments, which are almost always a spreadsheet in disguise.

An export named `.xls` that is really an HTML document is the single most common
misdeclared attachment in a procurement mailbox - it is what ERP and portal
"export to Excel" buttons produce. Left unread it looks like an RFQ with no
items, so the table has to come out of the markup exactly as it would out of a
worksheet.

selectolax is already a dependency, and `html_to_text` already handles the prose.
"""

import logging

from selectolax.parser import HTMLParser

from src.domain.preprocessing.html import html_to_text
from src.infrastructure.documents.budget import Budget
from src.infrastructure.documents.models import UNREADABLE, Document, Grid

logger = logging.getLogger(__name__)

_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def read_html(document: Document, data: bytes, budget: Budget) -> Document:
    markup = _decode(data)

    try:
        tree = HTMLParser(markup)
    except Exception as error:  # noqa: BLE001
        document.warn(UNREADABLE)
        logger.debug("selectolax could not parse %s: %s", document.filename, error)
        return document

    for index, table in enumerate(tree.css("table"), start=1):
        grid = Grid(
            origin=f"{document.origin}#table{index}",
            rows=_rows(table, budget),
        )
        if not grid.is_empty:
            document.grids.append(grid)

    document.text = html_to_text(markup).strip()
    return document


def _rows(table, budget: Budget) -> list[list[str | None]]:
    """Cells in document order.

    `colspan` is deliberately not expanded: a merged header spanning three
    columns would otherwise be duplicated into three, and the column-mapping
    step reads the header row literally.
    """
    collected: list[list[str | None]] = []

    for row in table.css("tr"):
        if len(collected) >= budget.max_spreadsheet_rows:
            break
        cells = row.css("th, td")[: budget.max_spreadsheet_cols]
        collected.append([cell.text(strip=True) or None for cell in cells])

    return collected


def _decode(data: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")
