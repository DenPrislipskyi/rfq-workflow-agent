"""Turn a finished RFQ into the blank template the agent fills.

    uv run python -m src.tools.make_output_template "docs/DANT260189 1 (1).xlsx"

The team has no empty version of the output file - every copy they keep is one
somebody already filled in. So the blank one is made by clearing a filled one,
which is also why this is a tool rather than a one-off: the next time they send
a finished RFQ with a changed layout, the blank is one command away.

Cleared: the form's own cells, every item row, and the shared strings none of
them point at any more. Kept byte for byte: the labels, the merges, the styles,
the drawing, the item headings on row 23.
"""

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

from src.infrastructure.excel import BLANK, Template
from src.services.workbook import (
    BRANCH_CELL,
    CELLS,
    FIRST_ITEM_ROW,
    ITEM_COLUMNS,
    RECEIVED_CELLS,
    REMARKS_CELL,
    SUBJECT_CELL,
)

# Every cell the agent ever writes, so that a template made from a filled RFQ
# comes out with none of that RFQ left in it.
MANAGED = [*CELLS.values(), BRANCH_CELL, SUBJECT_CELL, REMARKS_CELL, *RECEIVED_CELLS]

# The person who last saved the file we are clearing. Their name must not end
# up on every RFQ the agent generates.
_CREATOR = re.compile(rb"<dc:creator>[^<]*</dc:creator>")
_MODIFIED_BY = re.compile(rb"<cp:lastModifiedBy>[^<]*</cp:lastModifiedBy>")
AUTHOR = "KASS RFQ agent"

SHEET = "xl/worksheets/sheet1.xml"
STRINGS = "xl/sharedStrings.xml"

_SHARED_CELL = re.compile(rb'<c\b[^>]*\bt="s"[^>]*>.*?</c>', re.DOTALL)
_INDEX = re.compile(rb"<v>(\d+)</v>")
_SI = re.compile(rb"<si>.*?</si>|<si/>", re.DOTALL)
_SST_OPEN = re.compile(rb"<sst\b[^>]*>")
_COUNTS = re.compile(rb'\s(?:unique)?[Cc]ount="\d+"')


def blank(filled: bytes) -> bytes:
    """The same workbook with nothing filled in."""
    last_row = _last_row(filled)
    cells = {reference: BLANK for reference in MANAGED}
    for row in range(FIRST_ITEM_ROW, last_row + 1):
        cells |= {f"{column}{row}": BLANK for column in ITEM_COLUMNS.values()}

    return _anonymous(_pruned(Template(filled).fill(cells)))


def _last_row(data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        sheet = book.read(SHEET)
    return max(int(number) for number in re.findall(rb'<row\b[^>]*r="(\d+)"', sheet))


def _pruned(data: bytes) -> bytes:
    """The same workbook without the shared strings nothing points at.

    Clearing a cell leaves its text behind in `sharedStrings.xml` - invisible in
    Excel, plainly readable in the file. This template is committed to the
    repository and the file it is made from is somebody's real RFQ: a vessel
    name, a sender code, eleven item descriptions. A blank-looking file is
    exactly how that ends up in version control.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        sheet = book.read(SHEET)
        strings = book.read(STRINGS)

    entries = _SI.findall(strings)
    kept: dict[int, int] = {}
    references = 0

    def renumber(cell: re.Match[bytes]) -> bytes:
        nonlocal references
        found = _INDEX.search(cell.group(0))
        if found is None:
            return cell.group(0)
        references += 1
        old = int(found.group(1))
        index = kept.setdefault(old, len(kept))
        return _INDEX.sub(f"<v>{index}</v>".encode(), cell.group(0), count=1)

    sheet = _SHARED_CELL.sub(renumber, sheet)

    body = b"".join(entries[old] for old in sorted(kept, key=lambda old: kept[old]))
    opening = _COUNTS.sub(b"", _SST_OPEN.search(strings).group(0))  # ty: ignore
    opening = opening[:-1] + f' count="{references}" uniqueCount="{len(kept)}">'.encode()
    strings = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + opening + body + b"</sst>"
    )

    return _replacing(data, {SHEET: sheet, STRINGS: strings})


def _replacing(data: bytes, parts: dict[str, bytes]) -> bytes:
    """The same archive with some parts swapped, everything else untouched."""
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
        for entry in source.infolist():
            copy.writestr(entry, parts.get(entry.filename) or source.read(entry.filename))
    return buffer.getvalue()


def _anonymous(data: bytes) -> bytes:
    """The same file with the previous author's name taken out of it."""
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        core = book.read("docProps/core.xml")

    core = _CREATOR.sub(f"<dc:creator>{AUTHOR}</dc:creator>".encode(), core)
    core = _MODIFIED_BY.sub(f"<cp:lastModifiedBy>{AUTHOR}</cp:lastModifiedBy>".encode(), core)
    return _replacing(data, {"docProps/core.xml": core})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("filled", type=Path, help="a finished RFQ to clear")
    # Never `config/rfq_output_template.xlsx`: a bare run would replace the form
    # the service ships with, and that file is checked into the image. The new
    # blank lands beside the other generated files and is copied into `config/`
    # by hand, once somebody has opened it.
    parser.add_argument(
        "--out", type=Path, default=Path("data/rfq_output_template.xlsx"),
        help="where to write the blank; copy it into config/ yourself",
    )
    args = parser.parse_args(argv)

    if not args.filled.is_file():
        print(f"No such file: {args.filled}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(blank(args.filled.read_bytes()))
    print(f"{args.filled} -> {args.out}  ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
