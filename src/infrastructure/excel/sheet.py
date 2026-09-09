"""Writing values into a worksheet part without rewriting the worksheet.

The master's `sheet1.xml` is 126.7 MB - 5.42 million cells of which 2.14% hold
anything, the residue of a round trip through Google Sheets. Everything this
stage writes sits in the first rows, so the part is edited in place: the cells
named are replaced, and every other byte, including the two formulas the task
forbids touching, is passed through untouched.

That is also what keeps the guarantee testable. "Only these cells changed" is a
byte comparison against the master, which no library that re-serializes the
workbook could offer.
"""

import re
from collections.abc import Iterator, Mapping

from src.infrastructure.excel.exceptions import TemplateShapeError
from src.infrastructure.excel.styles import StyleTable
from src.infrastructure.excel.values import (
    Blank,
    CellValue,
    Day,
    Moment,
    Number,
    Text,
    excel_serial,
    excel_timestamp,
)

_ROW = re.compile(rb"<row\b[^>]*>")
_SHEET_DATA_END = b"</sheetData>"
_ROW_NUMBER = re.compile(rb'\sr="(\d+)"')
_CELL = re.compile(rb"<c\b[^>]*>")
_REFERENCE = re.compile(rb'\sr="([A-Z]+)(\d+)"')
_STYLE = re.compile(rb'\ss="(\d+)"')

_CELL_REFERENCE = re.compile(r"^([A-Z]+)([1-9]\d*)$")

# Excel refuses to open a file whose cell holds more than this.
MAX_TEXT = 32_767

# XML 1.0 has no way to encode these, and customer text does contain them -
# a form feed out of a PDF, a stray NUL out of a badly written CSV.
_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def write_cells(
    sheet: bytes, values: Mapping[str, CellValue], styles: StyleTable
) -> list[bytes | memoryview]:
    """The worksheet part with those cells written, as chunks to be streamed.

    Chunks rather than one buffer: the last of them is a view onto the hundred
    megabytes of the master that nothing touched, and copying it to say so would
    double the memory this costs.
    """
    wanted = _by_row(values)
    if not wanted:
        return [memoryview(sheet)]

    last = max(wanted)
    chunks: list[bytes | memoryview] = []
    cursor = 0
    highest = 0
    view = memoryview(sheet)

    for match in _ROW.finditer(sheet):
        number = _row_number(match.group(0))
        highest = max(highest, number)
        if number not in wanted:
            if number > last:
                break
            continue

        start, end = _row_span(sheet, match)
        chunks.append(view[cursor:start])
        chunks.append(_rewritten_row(sheet[start:end], wanted.pop(number), styles))
        cursor = end
        if number >= last:
            break

    if not wanted:
        chunks.append(view[cursor:])
        return chunks

    if min(wanted) <= highest:
        # A gap in the middle. Filling it would mean putting a row out of
        # order, which Excel reads as a corrupt file, so nothing is written.
        raise TemplateShapeError(
            f"the sheet skips row {min(wanted)}, so its cells would be lost"
        )

    # Rows past the end of the sheet: an item list longer than the template was
    # last saved with. 154 rows are laid out and a real RFQ arrived with 211.
    end_of_data = sheet.rindex(_SHEET_DATA_END)
    chunks.append(view[cursor:end_of_data])
    for number in sorted(wanted):
        chunks.append(_appended_row(number, wanted[number], styles))
    chunks.append(view[end_of_data:])
    return chunks


def _appended_row(
    number: int, targets: dict[int, tuple[str, CellValue]], styles: StyleTable
) -> bytes:
    """A row the sheet did not have, with only the cells we are writing.

    No style of its own, which is what the template's own item rows have: the
    grid below the headings is unformatted in both finished RFQs we were given.
    """
    return _rewritten_row(f'<row r="{number}"/>'.encode(), targets, styles)


def _by_row(values: Mapping[str, CellValue]) -> dict[int, dict[int, tuple[str, CellValue]]]:
    """Row number -> column number -> the cell to write there."""
    rows: dict[int, dict[int, tuple[str, CellValue]]] = {}
    for reference, value in values.items():
        match = _CELL_REFERENCE.match(reference)
        if match is None:
            raise ValueError(f"{reference!r} is not a cell reference")
        letters, number = match.groups()
        rows.setdefault(int(number), {})[column_number(letters)] = (reference, value)
    return rows


def _row_span(sheet: bytes, match: re.Match[bytes]) -> tuple[int, int]:
    """Where this row's markup starts and ends, closing tag included."""
    if match.group(0).endswith(b"/>"):
        return match.start(), match.end()
    return match.start(), sheet.index(b"</row>", match.end()) + len(b"</row>")


def _rewritten_row(
    row: bytes, targets: dict[int, tuple[str, CellValue]], styles: StyleTable
) -> bytes:
    """One row with the named cells replaced, and the rest of it left alone."""
    if row.endswith(b"/>"):
        # An empty row, written by Excel as a single tag. Give it a body to
        # put the cells in.
        row = row[:-2] + b"></row>"

    existing = list(_cells(row))
    body_ends = row.rindex(b"</row>")
    at_column = {column: span for column, span in existing}

    # Where each value goes: over the cell that is there, or in front of the
    # first cell that comes after it. Cells have to stay in column order.
    edits = []
    for column, (reference, value) in sorted(targets.items()):
        if column in at_column:
            start, end = at_column[column]
            edits.append((start, end, reference, value, _style_of(row[start:end])))
        else:
            after = next((s for c, (s, _) in existing if c > column), body_ends)
            edits.append((after, after, reference, value, None))

    out = bytearray()
    cursor = 0
    for start, end, reference, value, style in sorted(edits, key=lambda edit: edit[:2]):
        out += row[cursor:start]
        out += _cell(reference, value, style, styles)
        cursor = end
    out += row[cursor:]
    return bytes(out)


def _cells(row: bytes) -> Iterator[tuple[int, tuple[int, int]]]:
    """Every cell in the row as (column number, where its markup lies)."""
    for match in _CELL.finditer(row):
        reference = _REFERENCE.search(match.group(0))
        if reference is None:
            continue
        if match.group(0).endswith(b"/>"):
            span = (match.start(), match.end())
        else:
            span = (match.start(), row.index(b"</c>", match.end()) + len(b"</c>"))
        yield column_number(reference.group(1).decode()), span


def _cell(reference: str, value: CellValue, style: int | None, styles: StyleTable) -> bytes:
    """One cell's markup. The style the master gave the cell is kept."""
    match value:
        case Blank():
            return f'<c r="{reference}"{_style(style)}/>'.encode()
        case Text(text):
            return (
                f'<c r="{reference}"{_style(style)} t="inlineStr">'
                f'<is><t xml:space="preserve">{_escaped(text)}</t></is></c>'
            ).encode()
        case Number(number):
            return f'<c r="{reference}"{_style(style)}><v>{_written(number)}</v></c>'.encode()
        case Day(day):
            formatted = styles.for_date(style, with_time=False)
            return f'<c r="{reference}" s="{formatted}"><v>{excel_serial(day)}</v></c>'.encode()
        case Moment(moment):
            formatted = styles.for_date(style, with_time=True)
            serial = _written(excel_timestamp(moment))
            return f'<c r="{reference}" s="{formatted}"><v>{serial}</v></c>'.encode()
    raise ValueError(f"{value!r} is not a cell value")


def _style(style: int | None) -> str:
    return f' s="{style}"' if style is not None else ""


def _style_of(cell: bytes) -> int | None:
    found = _STYLE.search(cell)
    return int(found.group(1)) if found else None


def _row_number(tag: bytes) -> int:
    found = _ROW_NUMBER.search(tag)
    if found is None:
        raise TemplateShapeError(f"a row without a number: {tag!r}")
    return int(found.group(1))


def _written(value: float) -> str:
    """`2`, not `2.0` - a quantity that came in whole goes out whole."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _escaped(text: str) -> str:
    cleaned = _FORBIDDEN.sub("", text)[:MAX_TEXT]
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def column_number(letters: str) -> int:
    """`A` is 1, `Z` is 26, `AA` is 27. Ordering columns needs the number, not
    the letters: `AA` sorts before `B` as text and after it on the sheet."""
    number = 0
    for letter in letters:
        number = number * 26 + (ord(letter) - ord("A") + 1)
    return number


def column_letters(number: int) -> str:
    """The inverse, for naming a column in a warning a person will read."""
    letters = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


__all__ = ["column_letters", "column_number", "write_cells"]
