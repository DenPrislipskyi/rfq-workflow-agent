"""A spreadsheet loaded once and never modified, only copied and filled.

Held as the file's own bytes rather than as a parsed object. Everything we write
lives in the first rows of one sheet; every other part - the labels, thirty-six
merged ranges, the styles, a drawing, and in the master workbook a VBA project
and an array formula - is copied out byte for byte.

That is the whole argument against openpyxl here. It would re-serialize the
book, and fidelity for that list is not something it promises. What it buys is
a claim that can be tested: a filled copy differs from the template in the cells
that were filled and nowhere else, which is a byte comparison.
"""

import hashlib
import io
import logging
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path

from src.infrastructure.excel.exceptions import TemplateShapeError
from src.infrastructure.excel.sheet import write_cells
from src.infrastructure.excel.styles import StyleTable
from src.infrastructure.excel.values import CellValue

logger = logging.getLogger(__name__)

SHEET = "xl/worksheets/sheet1.xml"
STYLES = "xl/styles.xml"
WORKBOOK = "xl/workbook.xml"

# Two of the cells on the form are formulas: `A1` looks the customer's name up
# from the sender code, and `H10` restates `H12` in another time zone. Excel
# keeps the value it last calculated and will happily show a stale one, so the
# copy is told to recalculate everything the moment it opens.
_CALC_PR = re.compile(rb"<calcPr\b[^>]*?/>|<calcPr\b.*?</calcPr>", re.DOTALL)
_RECALCULATE = b'<calcPr fullCalcOnLoad="1"/>'


class Template:
    """A master workbook that can be filled without being changed."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._checksum = hashlib.sha256(data).hexdigest()
        with zipfile.ZipFile(io.BytesIO(data)) as book:
            names = set(book.namelist())
            missing = {SHEET, STYLES, WORKBOOK} - names
            if missing:
                raise TemplateShapeError(f"the workbook has no {', '.join(sorted(missing))}")
            self._workbook = _recalculating(book.read(WORKBOOK))

    @classmethod
    def load(cls, path: Path) -> "Template":
        """Read the master off disk. Does not keep the path: the bytes are the
        template, and a file replaced underneath us must not change our output
        without the checksum in the log changing too."""
        template = cls(path.read_bytes())
        logger.info(
            "Template %s loaded, %.1f MB, sha256 %s",
            path.name,
            len(template._data) / (1024 * 1024),
            template.checksum,
        )
        return template

    @property
    def checksum(self) -> str:
        """SHA-256 of the master. Goes on every journal line, so that a workbook
        that came out wrong can be traced to the master it came from."""
        return self._checksum

    def fill(self, cells: Mapping[str, CellValue]) -> bytes:
        """A copy of the template with those cells written, as a spreadsheet.

        Every entry keeps its own name, timestamp and compression, and every one
        but the three we have a reason to touch keeps its bytes as well.
        """
        with zipfile.ZipFile(io.BytesIO(self._data)) as master:
            styles = StyleTable(master.read(STYLES))
            # Written before the styles are: filling the sheet is what tells the
            # table which date formats this workbook turned out to need.
            chunks = write_cells(master.read(SHEET), cells, styles)

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
                for entry in master.infolist():
                    if entry.filename == SHEET:
                        with copy.open(entry, "w") as stream:
                            for chunk in chunks:
                                stream.write(chunk)
                    elif entry.filename == STYLES:
                        copy.writestr(entry, styles.to_xml())
                    elif entry.filename == WORKBOOK:
                        copy.writestr(entry, self._workbook)
                    else:
                        copy.writestr(entry, master.read(entry.filename))

        return buffer.getvalue()


def _recalculating(workbook: bytes) -> bytes:
    """The workbook part, told to recalculate its formulas when it opens."""
    if _CALC_PR.search(workbook):
        return _CALC_PR.sub(_RECALCULATE, workbook, count=1)
    return workbook.replace(b"</workbook>", _RECALCULATE + b"</workbook>", 1)
