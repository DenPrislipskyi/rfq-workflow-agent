"""B2, B3 and B5 in one: read a file, once.

The model is shown a file and answers everything about it at once - what it is,
and either what its columns mean or what its items say. There used to be two
calls to every file, the first asking "what is this" and the second "now name
the columns", both showing the same sample. Merging them cut an ordinary RFQ
from five model calls to three.

What did not merge is the division of labour. When the file has a table the
model names the columns and **code copies the rows**, because a model
transcribing 244 rows reorders and drops them while a `for` loop cannot, and
"preserve the original customer line-item order" is a Key Rule. Code also
checks the answer against the data before believing it.

No format is named anywhere in here. Stage A already reduced every attachment
to text, tables and images, so this reader sees the same three things whether
they came from a spreadsheet, a scan or a photograph.
"""

import logging

from src.infrastructure.documents.models import Document, Grid
from src.infrastructure.llm.client import LLM
from src.infrastructure.llm.exceptions import LLMError
from src.services.extraction.models import (
    MAPPING_UNVERIFIED,
    NO_ITEM_ROWS,
    NO_TABLE_FOUND,
    NOTHING_TO_READ,
    READ_FAILED,
    SEVERAL_TABLES,
    TRANSCRIPTION_PARTIAL,
    UNMAPPED_COLUMNS,
    DocumentRole,
    ItemField,
    LineItem,
    ReadDocument,
)
from src.services.extraction.prompt import (
    CONTAINERS,
    build_file_messages,
    build_items_messages,
)
from src.services.extraction.schemas import FileRead, ItemsFromText, ReadItem, TableMapping
from src.services.extraction.table import TableBlock, block_at, find_blocks
from src.services.extraction.text_parts import Part, split

logger = logging.getLogger(__name__)

# Of the values present in the column mapped to quantity. Below this the mapping
# is wrong, whatever the header said - customers do write "2 coil" in a quantity
# column, but they do not write "ROPE PP 24MM".
MIN_NUMERIC_SHARE = 0.9
# A description column empty in most rows is not the description column.
MIN_DESCRIPTION_SHARE = 0.5


class FileReader:
    """One attachment in, one answer out."""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    async def read(self, document: Document) -> ReadDocument:
        """What this file is, and whatever items are in it."""
        if not document.has_content:
            # A link, a file over the limit, a corrupt one - or a container,
            # whose contents became documents of their own and which has
            # nothing of its own to show.
            return ReadDocument(
                document=document,
                role=DocumentRole.EMPTY,
                warnings=[] if document.kind in CONTAINERS else [NOTHING_TO_READ],
            )

        blocks = _blocks(document)
        parts = split(document)

        answer, problem = await self._ask(document, blocks, parts)
        if answer is None:
            # Deliberately not "assume supporting": if this was the requisition,
            # guessing would drop every line item and say nothing about it.
            return ReadDocument(
                document=document, role=DocumentRole.UNREAD, warnings=[READ_FAILED]
            )

        found = ReadDocument(
            document=document,
            role=_role(document, blocks, answer),
            what=answer.what.strip(),
            facts=[fact.strip() for fact in answer.facts if fact.strip()],
        )
        if not found.holds_items:
            return found

        return (
            await self._from_tables(found, blocks, answer, problem)
            if found.role is DocumentRole.ITEM_GRID
            else await self._from_parts(found, parts, answer)
        )

    async def _ask(
        self, document: Document, blocks: list[TableBlock], parts: list[Part]
    ) -> tuple[FileRead | None, str]:
        """Ask, verify any mapping, and ask once more with the failure spelled out.

        One retry, not a loop: a model that cannot name the columns of a table
        it has now seen twice will not do better on a third showing.
        """
        problem: str | None = None
        answer: FileRead | None = None

        for attempt in (1, 2):
            try:
                answer = (
                    await self._llm.invoke(
                        build_file_messages(document, blocks, parts, problem), FileRead
                    )
                ).value
            except LLMError as error:
                logger.warning("Could not read %s: %s", document.origin, error)
                return None, ""

            if not (answer.has_item_list and blocks):
                return answer, ""

            problem = _first_problem(blocks, answer.tables)
            if not problem:
                return answer, ""

            logger.info("Read %d of %s rejected: %s", attempt, document.origin, problem)

        return answer, problem or ""

    async def _from_tables(
        self,
        found: ReadDocument,
        blocks: list[TableBlock],
        answer: FileRead,
        problem: str,
    ) -> ReadDocument:
        """Copy the rows the model just named the columns of."""
        if problem:
            # The retry did not hold up either. Rows are not copied on a mapping
            # known to be wrong - that is worse than copying none.
            logger.warning("Mapping for %s stays unverified: %s", found.origin, problem)
            return _with(found, warnings=[MAPPING_UNVERIFIED])

        items: list[LineItem] = []
        unmapped: list[str] = []
        warnings: list[str] = [SEVERAL_TABLES] if len(blocks) > 1 else []

        for number, block in enumerate(blocks, start=1):
            mapping = _mapping_for(answer.tables, number)
            if mapping is None:
                continue
            block = block_at(block.grid, mapping.header_row - 1) or block
            items += _copy_rows(block, mapping, start_at=len(items) + 1)
            unmapped += [
                column for column in _unmapped(block, mapping) if column not in unmapped
            ]

        if unmapped:
            warnings.append(UNMAPPED_COLUMNS)
        if not items:
            warnings.append(NO_ITEM_ROWS)

        return _with(found, items=items, unmapped_columns=unmapped, warnings=warnings)

    async def _from_parts(
        self, found: ReadDocument, parts: list[Part], answer: FileRead
    ) -> ReadDocument:
        """Take the first part's transcription, then continue through the rest.

        The merged call read part one while working out what the file was. Two
        things bring us back here: a document too long for one call, and a
        model that recognised the list without copying any of it.
        """
        if not parts:
            return _with(found, warnings=[NO_ITEM_ROWS])

        warnings: list[str] = []
        read = list(answer.items)
        if not read:
            # Measured on a real scanned requisition: the answer said
            # "Requisition listing 11 IMPA washer items" and carried none of
            # them. One question at a time recovers it - `build_items_messages`
            # asks for the list and nothing else, where the merged call was
            # also working out what the file was.
            #
            # The same second chance the table path has always had: there, code
            # checks the mapping against the columns and asks again with the
            # failure spelled out. Here there is nothing to check the model
            # against except whether it answered at all.
            logger.info("%s holds a list and came back empty, asking again", found.origin)
            again = await self._continue(parts[0], "")
            if again is None:
                warnings.append(TRANSCRIPTION_PARTIAL)
            read = again or []

        items = [
            _line_item(item, parts[0], number=index)
            for index, item in enumerate(read, start=1)
        ]
        seen = {_identity(item) for item in read}
        previous = parts[0].tail

        for part in parts[1:]:
            more = await self._continue(part, previous)
            if more is None:
                warnings.append(TRANSCRIPTION_PARTIAL)
                continue

            for item in more:
                key = _identity(item)
                if key in seen:
                    continue
                seen.add(key)
                items.append(_line_item(item, part, number=len(items) + 1))
            previous = part.tail

        if not items:
            warnings.append(NO_ITEM_ROWS)

        logger.info("Transcribed %d item(s) from %s", len(items), found.origin)
        return _with(found, items=items, warnings=warnings)

    async def _continue(self, part: Part, already_read: str) -> list[ReadItem] | None:
        try:
            answer = await self._llm.invoke(
                build_items_messages(part.texts, part.images, already_read), ItemsFromText
            )
        except LLMError as error:
            logger.warning("Could not transcribe %s: %s", part.label, error)
            return None
        return answer.value.items


# --- deciding what the file is --------------------------------------------


def _blocks(document: Document) -> list[TableBlock]:
    """Every table in the file, found by code before the model is asked.

    A grid the word list did not recognise still gets offered: its first row is
    taken as a provisional header, and the prompt says so. That is how a
    requisition headed "Pos | Artikel | Menge" is read at all.
    """
    grids = [*document.grids, *(table for page in document.pages for table in page.tables)]
    found = [block for grid in grids for block in find_blocks(grid)]
    if found:
        return found

    return [block for grid in grids if _tabular(grid) and (block := block_at(grid, 0))]


def _tabular(grid: Grid) -> bool:
    """Enough of a rectangle to be worth guessing at. One column is prose."""
    return grid.width >= 2 and grid.height >= 2


def _role(document: Document, blocks: list[TableBlock], answer: FileRead) -> DocumentRole:
    """The model says whether the items are here; code says in what shape.

    Both halves are needed. "This is a requisition" does not say whether the
    rows can be copied by a loop or have to be read off a photograph, and that
    is the difference between the cheap path and the careful one.
    """
    if not answer.has_item_list:
        return DocumentRole.SUPPORTING
    return DocumentRole.ITEM_GRID if blocks else DocumentRole.ITEM_TEXT


def _mapping_for(mappings: list[TableMapping], number: int) -> TableMapping | None:
    """The mapping the model gave for this table, or the only one it gave."""
    if len(mappings) == 1 and number == 1:
        return mappings[0]
    return next((one for one in mappings if one.table == number), None)


# --- checking the model's answer ------------------------------------------


def _first_problem(blocks: list[TableBlock], mappings: list[TableMapping]) -> str:
    if not mappings:
        return "no columns were named for a file said to hold the item list"

    for number, block in enumerate(blocks, start=1):
        mapping = _mapping_for(mappings, number)
        if mapping is None:
            continue
        if problem := verify(block_at(block.grid, mapping.header_row - 1) or block, mapping):
            return problem
    return ""


def verify(block: TableBlock, mapping: TableMapping) -> str:
    """Check the mapping against the data. Empty string means it holds.

    Worth more than any amount of extra context: a model can be shown the whole
    file and still call `PACK SIZE` the quantity, and only the numbers in the
    column say otherwise.
    """
    letters = [one.column for one in mapping.assignments]
    if len(letters) != len(set(letters)):
        return "the same column was given two fields"

    fields = [one.field for one in mapping.assignments]
    if len(fields) != len(set(fields)):
        return "the same field was taken from two columns"

    for letter in letters:
        if block.profile_for(letter) is None:
            return f"column {letter} is not in this table"

    if empty := [
        letter
        for letter in letters
        if (column := block.profile_for(letter)) and column.is_empty
    ]:
        return f"column {', '.join(empty)} is empty in every row"

    if problem := _share(block, mapping, ItemField.QUANTITY, MIN_NUMERIC_SHARE, "numeric"):
        return problem
    return _share(block, mapping, ItemField.DESCRIPTION, MIN_DESCRIPTION_SHARE, "filled")


def _share(
    block: TableBlock, mapping: TableMapping, field: ItemField, minimum: float, what: str
) -> str:
    letter = mapping.column_for(field)
    column = block.profile_for(letter) if letter else None
    if column is None:
        return ""

    share = column.numeric_share if what == "numeric" else column.fill_share
    if share >= minimum:
        return ""
    return (
        f"column {letter} was given to {field.value}, but only "
        f"{share:.0%} of its values are {what} (expected at least {minimum:.0%})"
    )


# --- moving the values ----------------------------------------------------


def _copy_rows(block: TableBlock, mapping: TableMapping, *, start_at: int) -> list[LineItem]:
    """The loop. Nothing here interprets a value; it only moves it."""
    columns = {one.field: block.profile_for(one.column) for one in mapping.assignments}
    quantified = columns.get(ItemField.QUANTITY) is not None
    items: list[LineItem] = []

    for number, cells in block.data_rows():
        values = {
            field: _cell(cells, column.index)
            for field, column in columns.items()
            if column is not None
        }
        # A row with neither a description nor a code is a spacer or a total.
        if not values.get(ItemField.DESCRIPTION) and not values.get(
            ItemField.CUSTOMER_ITEM_CODE
        ):
            continue
        # Requisitions group items under catalogue headings written into the
        # description column - "CHEMICALS [65]". They read as items and are not:
        # nobody is ordering a quantity of them.
        if quantified and not values.get(ItemField.QUANTITY):
            continue

        items.append(
            LineItem(
                # The template numbers its own rows, so the customer's numbering
                # is read but not carried: gaps and restarts in it are theirs.
                sr_no=start_at + len(items),
                description=values.get(ItemField.DESCRIPTION),
                customer_item_code=values.get(ItemField.CUSTOMER_ITEM_CODE),
                quantity=values.get(ItemField.QUANTITY),
                uom=values.get(ItemField.UOM),
                source=f"{block.grid.origin} row {number + 1}",
            )
        )

    return items


def _line_item(found: ReadItem, part: Part, *, number: int) -> LineItem:
    """A transcribed item, with a source it is allowed to have named."""
    cited = (found.page or "").strip()
    return LineItem(
        sr_no=number,
        description=_clean(found.description),
        customer_item_code=_clean(found.customer_item_code),
        quantity=_clean(found.quantity),
        uom=_clean(found.uom),
        source=cited if cited in part.labels else part.label,
    )


def _unmapped(block: TableBlock, mapping: TableMapping) -> list[str]:
    """Columns that held data and went nowhere. Reported, never dropped quietly."""
    return [
        f"{column.letter} ({column.header})" if column.header else column.letter
        for column in block.profiles
        if not column.is_empty and column.letter not in mapping.mapped_columns
    ]


def _identity(found: ReadItem) -> tuple[str, str, str]:
    """What makes two transcribed rows the same row.

    Case and spacing are ignored: the overlap between two parts is the same text
    read twice, and a model does not retype it identically.
    """
    return tuple(
        " ".join((value or "").lower().split())
        for value in (found.customer_item_code, found.description, found.quantity)
    )  # ty: ignore


def _with(found: ReadDocument, **changes) -> ReadDocument:
    return ReadDocument(
        document=found.document,
        role=found.role,
        what=found.what,
        facts=found.facts,
        items=changes.get("items", found.items),
        unmapped_columns=changes.get("unmapped_columns", found.unmapped_columns),
        warnings=changes.get("warnings", found.warnings),
    )


def _cell(row: list[str | None], index: int) -> str | None:
    return row[index] if index < len(row) else None


def _clean(value: str | None) -> str | None:
    return value.strip() or None if value else None
