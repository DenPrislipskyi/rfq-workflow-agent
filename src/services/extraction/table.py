"""Everything about a customer's table that code can work out on its own.

Which row is the header, whether the sheet holds one table or three stacked on
top of each other, and what each column actually contains. None of it needs a
model, and all of it is exact - the cells are already in memory from stage A.

This is what makes the mapping call cheap and accurate at the same time. Three
sample rows would hide a `REMARKS` column filled in 7 rows out of 244, and would
not separate `QTY` from `PACK SIZE`; the whole file would cost a hundred times
more and answer no better. A profile says both in three hundred tokens.
"""

import re
from dataclasses import dataclass
from typing import Iterator

from src.infrastructure.documents.models import Grid

# Words that appear in the header row of an item list, in the spellings the
# corpus actually uses. Only for *finding* the header row - deciding what each
# column means is the model's job, because "ITEM" is a serial number in one
# file and a description in the next.
HEADER_WORDS = frozenset(
    {
        "code", "item", "items", "item code", "item no", "itemno",
        "impa", "issa", "part", "part no", "partno", "part number", "ref",
        "description", "desc", "item description", "material", "goods",
        "qty", "q'ty", "qnty", "quantity", "req qty", "required",
        "uom", "u/m", "unit", "units", "unit of measure", "measure", "item unit",
        "sr", "sr no", "sl", "sl no", "s/n", "no", "no.", "pos", "line",
        "remark", "remarks", "note", "notes", "maker", "brand",
    }
)

# Two hits, not one: a data row that happens to hold the word "item" must not
# be mistaken for a header, and every real header row carries at least an
# identifier and a description or a quantity.
MIN_HEADER_HITS = 2

# A header past this point is not a header - it is data that looks like one.
MAX_HEADER_SCAN_ROWS = 30

# Leading digits are enough to call a value numeric: customers write "2" and
# "2 coil" in the same column, and both are quantities.
_NUMERIC = re.compile(r"^\s*[\d]+([.,]\d+)?")
_PUNCTUATION = re.compile(r"[^a-z0-9/' ]+")

SAMPLES_PER_COLUMN = 5
# A column filled this rarely is invisible in any sample taken from the top,
# which is exactly the kind of column that gets mapped wrong.
RARE_COLUMN_SHARE = 0.2


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """What one column holds, measured over every data row of a block."""

    letter: str
    index: int
    header: str | None
    total: int
    filled: int
    numeric: int
    longest: int
    distinct: int
    samples: list[str]

    @property
    def fill_share(self) -> float:
        return self.filled / self.total if self.total else 0.0

    @property
    def numeric_share(self) -> float:
        """Of the values that are there. An empty column is not "0% numeric"."""
        return self.numeric / self.filled if self.filled else 0.0

    @property
    def is_empty(self) -> bool:
        return self.filled == 0

    @property
    def is_rare(self) -> bool:
        return 0 < self.fill_share <= RARE_COLUMN_SHARE

    def render(self) -> str:
        """One line for the prompt. Wide enough to read, short enough to send."""
        if self.is_empty:
            return f"  {self.letter}  {self._name():<22} empty"

        kind = "numbers" if self.numeric_share > 0.9 else "text"
        shape = f"{self.filled}/{self.total} filled, {kind}, {self.distinct} distinct"
        examples = ", ".join(self.samples[:SAMPLES_PER_COLUMN])
        return f"  {self.letter}  {self._name():<22} {shape:<38} {examples}"

    def _name(self) -> str:
        return self.header or "(no header)"


@dataclass(frozen=True, slots=True)
class TableBlock:
    """One header row and the data rows under it.

    A sheet can hold several - "DECK STORES" then "ENGINE STORES", each with its
    own header. Reading only the first would drop half the RFQ, and nothing in
    the file says there is a second one except the repeated header.
    """

    grid: Grid
    header_row: int
    first_data_row: int
    last_data_row: int
    profiles: list[ColumnProfile]

    @property
    def title(self) -> str:
        """The label above the header, if there is one: "DECK STORES"."""
        for index in range(self.header_row - 1, max(self.header_row - 3, -1), -1):
            if cells := [cell for cell in self.grid.rows[index] if cell]:
                return " ".join(cells)
        return ""

    @property
    def height(self) -> int:
        return self.last_data_row - self.first_data_row + 1

    def data_rows(self) -> Iterator[tuple[int, list[str | None]]]:
        """Every data row with its row number in the grid, in original order."""
        for index in range(self.first_data_row, self.last_data_row + 1):
            yield index, self.grid.rows[index]

    def profile_for(self, letter: str) -> ColumnProfile | None:
        return next((column for column in self.profiles if column.letter == letter), None)

    def render_profiles(self) -> str:
        return "\n".join(column.render() for column in self.profiles)

    def sample_rows(self) -> list[tuple[int, list[str | None]]]:
        """A spread of rows, not the first few.

        Head, middle and tail, plus any row that fills a rarely used column -
        the one a sample from the top would never show.
        """
        rows = list(self.data_rows())
        if len(rows) <= 3 * SAMPLES_PER_COLUMN:
            return rows

        wanted = {
            *range(SAMPLES_PER_COLUMN),
            *range(len(rows) // 2, len(rows) // 2 + SAMPLES_PER_COLUMN),
            *range(len(rows) - SAMPLES_PER_COLUMN, len(rows)),
            *self._rare_column_rows(rows),
        }
        return [rows[position] for position in sorted(wanted) if 0 <= position < len(rows)]

    def _rare_column_rows(self, rows: list[tuple[int, list[str | None]]]) -> set[int]:
        """One row per rarely filled column, so the model sees the column at all."""
        found: set[int] = set()
        for column in self.profiles:
            if not column.is_rare:
                continue
            for position, (_, cells) in enumerate(rows):
                if _cell(cells, column.index):
                    found.add(position)
                    break
        return found


def find_blocks(grid: Grid) -> list[TableBlock]:
    """Every table in this grid, in order. Empty when none looks like one."""
    starts = _header_rows(grid)
    if not starts:
        return []

    blocks = []
    for position, header_row in enumerate(starts):
        ends_before = starts[position + 1] if position + 1 < len(starts) else len(grid.rows)
        if block := _block(grid, header_row, ends_before):
            blocks.append(block)
    return blocks


def block_at(grid: Grid, header_row: int) -> TableBlock | None:
    """The block starting at this exact row, whether or not the word list agrees.

    `find_blocks` recognises a header by the words in it. When the model looks at
    the rows and names a different one, it saw something the word list could not
    - so the row is taken as given.
    """
    if not 0 <= header_row < len(grid.rows) - 1:
        return None

    ends_before = next(
        (index for index in _header_rows(grid) if index > header_row), len(grid.rows)
    )
    return _block(grid, header_row, ends_before)


def _block(grid: Grid, header_row: int, ends_before: int) -> TableBlock | None:
    """One block, or None when the header has no data under it.

    The table's extent is measured over the columns that actually carry a
    heading, and only those. Real RFQ workbooks park their dropdown lists in
    spare columns - one seen here runs 706 rows of UOM names beside a two-item
    requisition - and measuring the table by "the last row with anything in it"
    would stretch it over all 706 and bury the two items in reference data.
    """
    first = header_row + 1
    headers = grid.rows[header_row]
    headed = _table_columns(headers)
    if not headed:
        return None

    last = ends_before - 1
    while last >= first and _filled_in(grid.rows[last], headed) == 0:
        last -= 1

    # An item row fills at least two of the table's own cells - a code and a
    # description, or a description and a quantity. What trails a table is a
    # blank line, a "TOTAL" line, or the title of the next table, and all three
    # fill one. Trimming them keeps them out of the profile counts as well.
    rich = any(_filled_in(grid.rows[index], headed) >= 2 for index in range(first, last + 1))
    while last >= first and _is_tail_noise(grid.rows[last], headed, rich):
        last -= 1
    if last < first:
        return None

    rows = [grid.rows[index] for index in range(first, last + 1)]

    return TableBlock(
        grid=grid,
        header_row=header_row,
        first_data_row=first,
        last_data_row=last,
        profiles=[_profile(index, headers, rows) for index in headed],
    )


def _table_columns(headers: list[str | None]) -> list[int]:
    """Which columns belong to this table.

    Not "every column with something in the header row". A real workbook parks
    its dropdown lists in spare columns, and those lists have a value in every
    row - the header row included, where "BAG 16 KG" sits beside "ITEM CODE"
    and looks just as much like a heading.

    What separates them is position: the table is the unbroken run of filled
    heading cells around the ones recognised as headings. The lists sit past a
    gap, because a table and a parking lot are not adjacent by accident.
    """
    recognised = [
        index for index, cell in enumerate(headers) if _normalised(cell) in HEADER_WORDS
    ]
    if not recognised:
        # Nothing matched the word list, so this row was handed over as a guess
        # by `block_at`. Take it at face value.
        return [index for index, cell in enumerate(headers) if cell]

    left, right = min(recognised), max(recognised)
    while left > 0 and headers[left - 1]:
        left -= 1
    while right + 1 < len(headers) and headers[right + 1]:
        right += 1
    return [index for index in range(left, right + 1) if headers[index]]


def _header_rows(grid: Grid) -> list[int]:
    """Rows that read like column headings.

    The first one has to be near the top - past thirty rows a match is data that
    happens to say "item". A repeat further down is a second table, though, so
    the rest of the sheet is scanned for those.
    """
    return [
        index
        for index, row in enumerate(grid.rows)
        if (index < MAX_HEADER_SCAN_ROWS or _has_header_above(grid, index))
        and _header_hits(row) >= MIN_HEADER_HITS
    ]


def _has_header_above(grid: Grid, index: int) -> bool:
    return any(
        _header_hits(grid.rows[earlier]) >= MIN_HEADER_HITS
        for earlier in range(min(index, MAX_HEADER_SCAN_ROWS))
    )


def _profile(index: int, headers: list[str | None], rows: list[list[str | None]]) -> ColumnProfile:
    values = [value for row in rows if (value := _cell(row, index))]
    distinct = list(dict.fromkeys(values))

    return ColumnProfile(
        letter=column_letter(index),
        index=index,
        header=_cell(headers, index),
        total=len(rows),
        filled=len(values),
        numeric=sum(1 for value in values if _NUMERIC.match(value)),
        longest=max((len(value) for value in values), default=0),
        distinct=len(distinct),
        samples=[_short(value) for value in distinct[:SAMPLES_PER_COLUMN]],
    )


def is_numeric(value: str | None) -> bool:
    """What counts as a quantity. Used for profiling and for verification alike."""
    return bool(value) and bool(_NUMERIC.match(value))


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. The name a person reads in Excel."""
    letters = ""
    while True:
        index, remainder = divmod(index, 26)
        letters = chr(ord("A") + remainder) + letters
        if index == 0:
            return letters
        index -= 1


def _is_tail_noise(row: list[str | None], headed: list[int], rich: bool) -> bool:
    """Blank, or too thin to be an item in a table whose items are not thin."""
    filled = _filled_in(row, headed)
    return filled == 0 or (rich and filled < 2)


def _filled_in(row: list[str | None], columns: list[int]) -> int:
    """How many of these columns this row fills. Others are another region."""
    return sum(1 for index in columns if _cell(row, index))


def _header_hits(row: list[str | None]) -> int:
    """How many cells of this row read like column headings."""
    return sum(1 for cell in row if _normalised(cell) in HEADER_WORDS)


def _normalised(cell: str | None) -> str:
    if not cell:
        return ""
    return _PUNCTUATION.sub("", cell.strip().lower()).strip()


def _cell(row: list[str | None], index: int) -> str | None:
    return row[index] if index < len(row) else None


def _short(value: str, limit: int = 40) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
