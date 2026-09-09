"""Word and PowerPoint.

Both matter for one reason: their tables. An RFQ pasted into a Word document
keeps its item list in a real table, and a reader that only walks paragraphs
returns the covering letter and silently drops every line item. python-docx
exposes paragraphs and tables as separate collections, so a naive read of one
of them looks like it worked.

Legacy binary `.doc` has no clean pure-Python reader and is not handled here -
the loader flags it for a person. It is also rare: Exchange blocks `.docm` by
default, and plain `.doc` has been superseded for two decades.
"""

import io
import logging
from collections.abc import Iterable

import docx
import pptx

from src.infrastructure.documents.budget import Budget
from src.infrastructure.documents.models import UNREADABLE, Document, Grid

logger = logging.getLogger(__name__)


def read_docx(document: Document, data: bytes, budget: Budget) -> Document:
    try:
        opened = docx.Document(io.BytesIO(data))
    except Exception as error:  # noqa: BLE001 - python-docx raises package-level types
        document.warn(UNREADABLE)
        logger.debug("python-docx could not open %s: %s", document.filename, error)
        return document

    document.text = "\n".join(
        paragraph.text.strip() for paragraph in opened.paragraphs if paragraph.text.strip()
    )

    for index, table in enumerate(opened.tables, start=1):
        grid = Grid(
            origin=f"{document.origin}#table{index}",
            rows=_rows(table.rows, budget),
        )
        if not grid.is_empty:
            document.grids.append(grid)

    return document


def read_pptx(document: Document, data: bytes, budget: Budget) -> Document:
    """Slides, read for their text and tables.

    Decks turn up as RFQs more often than expected - a sourcing team pastes a
    requirement list into two slides and sends the deck.
    """
    try:
        presentation = pptx.Presentation(io.BytesIO(data))
    except Exception as error:  # noqa: BLE001
        document.warn(UNREADABLE)
        logger.debug("python-pptx could not open %s: %s", document.filename, error)
        return document

    lines: list[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
            if shape.has_table:
                grid = Grid(
                    origin=f"{document.origin}#slide{number}",
                    rows=_rows(shape.table.rows, budget),
                )
                if not grid.is_empty:
                    document.grids.append(grid)

    document.text = "\n".join(lines)
    return document


def _rows(rows: Iterable, budget: Budget) -> list[list[str | None]]:
    """Cap while iterating rather than by slicing.

    Neither library's row collection slices the way a list does - python-pptx
    raises outright - and iterating is the behaviour both of them do support.
    """
    collected: list[list[str | None]] = []
    for row in rows:
        if len(collected) >= budget.max_spreadsheet_rows:
            break
        cells = list(row.cells)[: budget.max_spreadsheet_cols]
        collected.append([cell.text.strip() or None for cell in cells])
    return collected
