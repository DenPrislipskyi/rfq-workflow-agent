"""One file, one call: what it is and what is in it.

What used to be three test files, because it used to be three services. The
model is faked throughout - under test is everything around it: the routing
built from its answer, whether the answer is checked before being believed, and
whether the loop that copies the rows keeps them intact.

No format appears in the reader, so none appears here either. Stage A already
reduced every attachment to text, tables and images.
"""

import pytest

from src.infrastructure.documents import Budget, DocumentLoader, SourceFile
from src.infrastructure.documents.models import Document, FileKind, Grid, ImageRef, Page
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.extraction import DocumentRole
from src.services.extraction.file_reader import FileReader, verify
from src.services.extraction.models import (
    MAPPING_UNVERIFIED,
    NO_ITEM_ROWS,
    NOTHING_TO_READ,
    READ_FAILED,
    SEVERAL_TABLES,
    TRANSCRIPTION_PARTIAL,
    UNMAPPED_COLUMNS,
    ItemField,
)
from src.services.extraction.prompt import WHOLE_TABLE_ROWS, build_file_messages
from src.services.extraction.schemas import (
    ColumnAssignment,
    FileRead,
    ItemsFromText,
    ReadItem,
    TableMapping,
)
from src.services.extraction.table import find_blocks
from tests import attachments_builder as build
from tests.fakes import BrokenLLM, FakeLLM

HEADER = ["ITEM", "IMPA", "DESCRIPTION", "QTY", "UNIT"]
ROWS = [
    ["1", "550101", "ROPE PP 24MM X 220M", "2", "coil"],
    ["2", "232101", "PAINT MARINE WHITE 20L", "5", "can"],
    ["3", "311204", "GASKET SET, PUMP", "1", "set"],
]

ROPE = ReadItem(description="Rope polypropylene 24mm x 220m", quantity="2 coils", uom="coil")
PAINT = ReadItem(description="Marine paint, white, 20L", quantity="5", uom="can")


def mapping(*, table: int = 1, header_row: int = 1, **fields: str) -> TableMapping:
    return TableMapping(
        table=table,
        header_row=header_row,
        assignments=[
            ColumnAssignment(column=letter, field=ItemField(field))
            for field, letter in fields.items()
        ],
    )


FULL = mapping(
    sr_no="A", customer_item_code="B", description="C", quantity="D", uom="E"
)


def sheet(rows: list[list[str | None]], name: str = "Sheet1") -> Document:
    return Document(
        filename="Requisition.xlsx",
        kind=FileKind.XLSX,
        grids=[Grid(origin=f"Requisition.xlsx#{name}", rows=rows, name=name)],
    )


def paged(*pages: str) -> Document:
    return Document(
        filename="enquiry.pdf",
        kind=FileKind.PDF,
        pages=[
            Page(origin=f"enquiry.pdf#p{number}", number=number, text=text)
            for number, text in enumerate(pages, start=1)
        ],
    )


def pictured(count: int = 1) -> Document:
    return Document(
        filename="scan.pdf",
        kind=FileKind.PDF,
        images=[
            ImageRef(origin=f"scan.pdf#p{n + 1}", media_type="image/jpeg", data=b"x")
            for n in range(count)
        ],
    )


class Answers:
    """Answers differently per call, so multi-part reads can be checked."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list = []

    async def invoke(self, messages, schema):
        self.calls.append(messages)
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return await FakeLLM(answer).invoke(messages, schema)


async def read(document: Document, *answers, llm=None):
    return await FileReader(llm or Answers(*answers)).read(document)


def grid_answer(*tables: TableMapping) -> FileRead:
    return FileRead(what="A requisition", has_item_list=True, tables=list(tables or (FULL,)))


def text_answer(*items: ReadItem) -> FileRead:
    return FileRead(what="A requisition", has_item_list=True, items=list(items))


SUPPORTING = FileRead(
    what="Photo of a VHF radio showing its nameplate",
    has_item_list=False,
    facts=["ICOM", "IC-M330GE", "S/N 12345678"],
)


# --- one call decides both the role and the contents ----------------------


async def test_a_table_is_read_by_code_once_the_columns_are_named():
    found = await read(sheet([HEADER, *ROWS]), grid_answer())

    assert found.role is DocumentRole.ITEM_GRID
    assert [item.description for item in found.items] == [
        "ROPE PP 24MM X 220M",
        "PAINT MARINE WHITE 20L",
        "GASKET SET, PUMP",
    ]


async def test_a_file_with_no_table_is_transcribed_in_the_same_answer():
    found = await read(paged("1) Rope 24mm - 2 coils\n2) Paint 20L - 5 cans"),
                       text_answer(ROPE, PAINT))

    assert found.role is DocumentRole.ITEM_TEXT
    assert [item.description for item in found.items] == [ROPE.description, PAINT.description]


async def test_a_photo_of_a_nameplate_is_supporting_not_an_item_source():
    """The case rules could never get right: a photo carrying real part numbers."""
    found = await read(pictured(), SUPPORTING)

    assert found.role is DocumentRole.SUPPORTING
    assert not found.holds_items
    assert found.items == []
    assert "IC-M330GE" in found.facts


async def test_one_call_per_file():
    """The whole point of the merge: asking what a file is and reading it are
    the same question about the same sample."""
    llm = Answers(grid_answer())

    await FileReader(llm).read(sheet([HEADER, *ROWS]))

    assert len(llm.calls) == 1


async def test_a_file_with_nothing_in_it_is_never_sent_to_a_model():
    llm = Answers(grid_answer())

    found = await FileReader(llm).read(Document(filename="link", kind=FileKind.UNKNOWN))

    assert found.role is DocumentRole.EMPTY
    assert NOTHING_TO_READ in found.warnings
    assert llm.calls == []


async def test_an_archive_is_not_reported_as_unreadable():
    """A zip holds no content of its own - what was inside it became documents
    of its own. Saying "nothing to read" about the wrapper is noise."""
    found = await read(Document(filename="bundle.zip", kind=FileKind.ZIP))

    assert found.role is DocumentRole.EMPTY
    assert found.warnings == []


async def test_a_model_that_cannot_answer_asks_for_a_person():
    """Assuming "supporting" would drop every line item and say nothing."""
    broken = BrokenLLM(LLMCallError("model", RuntimeError("provider down")))

    found = await read(sheet([HEADER, *ROWS]), llm=broken)

    assert found.role is DocumentRole.UNREAD
    assert found.needs_a_person
    assert READ_FAILED in found.warnings


# --- the loop that copies the rows ----------------------------------------


async def test_values_arrive_exactly_as_the_customer_wrote_them():
    """"2 coil" is not split - that is normalization's call, not a reader's."""
    found = await read(sheet([HEADER, ["1", "550101", "ROPE", "2 coil", "coil"]]),
                       grid_answer())

    assert found.items[0].quantity == "2 coil"
    assert found.items[0].customer_item_code == "550101"


async def test_the_template_numbers_the_rows_not_the_customer():
    rows = [["7", "550101", "ROPE", "2", "coil"], ["3", "232101", "PAINT", "5", "can"]]
    found = await read(sheet([HEADER, *rows]), grid_answer())

    assert [item.sr_no for item in found.items] == [1, 2]


async def test_each_item_cites_the_row_it_came_from():
    found = await read(sheet([HEADER, *ROWS]), grid_answer())

    assert found.items[0].source == "Requisition.xlsx#Sheet1 row 2"
    assert found.items[2].source == "Requisition.xlsx#Sheet1 row 4"


async def test_a_spacer_row_is_not_an_item():
    found = await read(sheet([HEADER, *ROWS, [None, None, None, "8", None]]), grid_answer())

    assert len(found.items) == 3


async def test_a_catalogue_heading_inside_the_table_is_not_an_item():
    """Real requisitions group items under headings written into the description
    column - "CHEMICALS [65]". Nobody is ordering a quantity of them."""
    rows = [
        HEADER,
        [None, None, "IMPA CATALOGUE [IMPA]", None, None],
        ["1", "NI55910", "ISOPROPANOL", "170", "LT"],
        [None, None, "CHEMICALS [65]", None, None],
        ["2", "5949-29-1", "CITRIC ACID", "10", "BAG"],
    ]
    found = await read(sheet(rows), grid_answer())

    assert [item.description for item in found.items] == ["ISOPROPANOL", "CITRIC ACID"]


async def test_a_thousand_rows_cost_one_call():
    """The whole reason the model names columns instead of transcribing."""
    rows = [[str(n), f"5501{n:03}", f"ROPE {n}", str(n), "coil"] for n in range(1, 1001)]
    llm = Answers(grid_answer())

    found = await FileReader(llm).read(sheet([HEADER, *rows]))

    assert len(found.items) == 1000
    assert len(llm.calls) == 1
    assert found.items[999].description == "ROPE 1000"


async def test_two_tables_in_one_sheet_are_read_into_one_numbered_list():
    rows = [["DECK STORES"], HEADER, ROWS[0], ROWS[1], [],
            ["ENGINE STORES"], HEADER, ROWS[2]]
    answer = grid_answer(mapping(table=1, header_row=2, description="C", quantity="D"),
                         mapping(table=2, header_row=7, description="C", quantity="D"))

    found = await read(sheet(rows), answer)

    assert [item.sr_no for item in found.items] == [1, 2, 3]
    assert found.items[2].description == "GASKET SET, PUMP"
    assert SEVERAL_TABLES in found.warnings


async def test_an_unmapped_column_that_held_data_is_reported():
    """A REMARKS column dropped in silence is information the customer sent."""
    rows = [[*row, "urgent"] for row in ROWS]
    found = await read(sheet([[*HEADER, "REMARKS"], *rows]), grid_answer())

    assert found.unmapped_columns == ["F (REMARKS)"]
    assert UNMAPPED_COLUMNS in found.warnings


# --- checking the answer before believing it ------------------------------


def block_of(rows):
    return find_blocks(Grid(origin="x", rows=rows))[0]


def test_a_quantity_column_that_is_not_numeric_is_rejected():
    """The check no amount of extra context replaces."""
    problem = verify(block_of([HEADER, *ROWS]), mapping(description="D", quantity="C"))

    assert "quantity" in problem
    assert "0%" in problem


def test_a_description_column_that_is_mostly_empty_is_rejected():
    rows = [["1", "550101", "ROPE", "2", "coil"], ["2", "232101", None, "5", "can"]]
    block = block_of([HEADER, *rows, ["3", "311204", None, "1", "set"]])

    assert "description" in verify(block, mapping(description="C", quantity="D"))


def test_two_fields_from_one_column_is_rejected():
    wrong = TableMapping(
        header_row=1,
        assignments=[
            ColumnAssignment(column="C", field=ItemField.DESCRIPTION),
            ColumnAssignment(column="C", field=ItemField.UOM),
        ],
    )

    assert verify(block_of([HEADER, *ROWS]), wrong) == "the same column was given two fields"


def test_a_column_that_is_not_in_the_table_is_rejected():
    assert "Z" in verify(block_of([HEADER, *ROWS]), mapping(description="Z"))


def test_an_always_empty_column_is_rejected():
    block = block_of([[*HEADER, "PRICE"], *[[*row, None] for row in ROWS]])

    assert "empty in every row" in verify(block, mapping(description="F"))


def test_a_correct_mapping_passes():
    assert verify(block_of([HEADER, *ROWS]), FULL) == ""


async def test_a_rejected_mapping_is_asked_again_with_the_reason():
    wrong = grid_answer(mapping(description="D", quantity="C"))
    llm = Answers(wrong, grid_answer())

    found = await read(sheet([HEADER, *ROWS]), llm=llm)

    assert len(llm.calls) == 2
    assert "did not hold up" in llm.calls[1][1][1]
    assert len(found.items) == 3


async def test_a_mapping_that_fails_twice_does_not_copy_the_rows():
    """Copying on a mapping known to be wrong is worse than copying nothing."""
    wrong = grid_answer(mapping(description="D", quantity="C"))
    llm = Answers(wrong, wrong)

    found = await read(sheet([HEADER, *ROWS]), llm=llm)

    assert found.items == []
    assert MAPPING_UNVERIFIED in found.warnings
    assert len(llm.calls) == 2, "one retry, not a loop"


async def test_a_file_said_to_hold_items_but_with_no_columns_named_is_refused():
    llm = Answers(FileRead(what="A requisition", has_item_list=True))

    found = await read(sheet([HEADER, *ROWS]), llm=llm)

    assert found.items == []
    assert MAPPING_UNVERIFIED in found.warnings


# --- documents too long for one call --------------------------------------


async def test_a_long_document_continues_after_the_first_answer():
    document = paged("x" * 3000, "y" * 3000)
    llm = Answers(text_answer(ROPE), ItemsFromText(items=[PAINT]))

    found = await FileReader(llm).read(document)

    assert len(llm.calls) == 2
    assert [item.description for item in found.items] == [ROPE.description, PAINT.description]


async def test_the_next_part_is_told_what_the_last_one_ended_with():
    """An item split across a page boundary must be recognisable as one seen."""
    llm = Answers(text_answer(ROPE), ItemsFromText(items=[PAINT]))

    await FileReader(llm).read(paged("x" * 3000, "y" * 3000))

    assert "<already_read>" in llm.calls[1][1][1]


async def test_an_item_transcribed_twice_across_a_boundary_is_kept_once():
    repeated = ReadItem(description="ROPE POLYPROPYLENE 24MM X 220M", quantity="2 coils")
    llm = Answers(text_answer(ROPE), ItemsFromText(items=[repeated, PAINT]))

    found = await FileReader(llm).read(paged("x" * 3000, "y" * 3000))

    assert len(found.items) == 2, "case and spacing differ; the item does not"
    assert [item.sr_no for item in found.items] == [1, 2]


async def test_one_failed_part_keeps_the_items_the_others_gave():
    """Losing page two is bad. Losing page one as well is worse."""

    class HalfBroken(Answers):
        async def invoke(self, messages, schema):
            self.calls.append(messages)
            if len(self.calls) == 2:
                raise LLMCallError("model", RuntimeError("provider down"))
            return await FakeLLM(text_answer(ROPE)).invoke(messages, schema)

    found = await FileReader(HalfBroken()).read(paged("x" * 3000, "y" * 3000))

    assert len(found.items) == 1
    assert TRANSCRIPTION_PARTIAL in found.warnings


async def test_a_file_called_a_list_that_came_back_empty_is_asked_again():
    """Measured on a real scanned requisition: the answer said "Requisition
    listing 11 IMPA washer items" and carried none of them. The merged call was
    also working out what the file was; asked for the list alone, the model
    produced it."""
    llm = Answers(text_answer(), ItemsFromText(items=[ROPE, PAINT]))

    found = await read(pictured(), llm=llm)

    assert len(llm.calls) == 2, "the second question asks for the items and nothing else"
    assert [item.description for item in found.items] == [
        "Rope polypropylene 24mm x 220m",
        "Marine paint, white, 20L",
    ]
    assert NO_ITEM_ROWS not in found.warnings


async def test_a_transcription_that_found_nothing_twice_says_so():
    """One retry, not a loop. A model that has now been shown the file twice
    and read nothing off it will not do better on a third showing."""
    llm = Answers(text_answer(), ItemsFromText(items=[]))

    found = await read(paged("just a covering letter"), llm=llm)

    assert len(llm.calls) == 2
    assert found.items == []
    assert NO_ITEM_ROWS in found.warnings


async def test_a_file_that_transcribed_on_the_first_ask_is_not_asked_twice():
    """The retry is for the failure, not a second opinion on every file."""
    llm = Answers(text_answer(ROPE))

    found = await read(paged("1) Rope 24mm"), llm=llm)

    assert len(llm.calls) == 1
    assert len(found.items) == 1


async def test_an_item_cites_the_page_it_names():
    answer = text_answer(ReadItem(description="Rope", page="enquiry.pdf#p1"))

    found = await read(paged("a"), answer)

    assert found.items[0].source == "enquiry.pdf#p1"


async def test_a_citation_that_names_nothing_real_falls_back_to_the_part():
    """A bad citation costs precision, never the item."""
    found = await read(paged("a"), text_answer(ReadItem(description="Rope", page="nowhere")))

    assert found.items[0].source == "enquiry.pdf#p1"


# --- what the model is shown ----------------------------------------------


def prompt(document: Document) -> str:
    from src.services.extraction.text_parts import split

    text = build_file_messages(document, _blocks(document), split(document))[1][1]
    return text if isinstance(text, str) else text[0]["text"]


def _blocks(document: Document):
    from src.services.extraction.file_reader import _blocks as found

    return found(document)


def test_a_small_table_is_shown_whole():
    """A couple of thousand tokens, and seeing every row answers better than
    any description of them."""
    text = prompt(sheet([HEADER, *ROWS]))

    assert "GASKET SET, PUMP" in text
    assert "measured over every data row" not in text


def test_a_large_table_is_shown_as_a_profile_and_a_spread_of_rows():
    """Past the threshold the arithmetic reverses: 244 rows is a hundred
    thousand tokens, and the profile says more than any sample could."""
    rows = [[str(n), f"5501{n:03}", f"ROPE {n}", str(n), "coil"]
            for n in range(1, WHOLE_TABLE_ROWS + 60)]
    text = prompt(sheet([HEADER, *rows]))

    assert "measured over every data row" in text
    assert "ROPE 1" in text and "ROPE 99" in text
    assert text.count("ROPE") < 40, "a spread of rows, not ninety-nine of them"


def test_the_profile_is_what_separates_two_numeric_columns():
    """`QTY` has sixty distinct values and `PACK` has four. No sample shows that."""
    rows = [[str(n), f"ROPE {n}", str(1 + n % 60), str(1 + n % 4)]
            for n in range(1, WHOLE_TABLE_ROWS + 60)]
    text = prompt(sheet([["ITEM", "DESCRIPTION", "QTY", "PACK"], *rows]))

    assert "60 distinct" in text
    assert "4 distinct" in text


def test_a_file_with_images_sends_them_as_image_blocks():
    from src.services.extraction.text_parts import split

    document = pictured()
    _, (_, content) = build_file_messages(document, [], split(document))

    assert isinstance(content, list)
    assert content[1]["type"] == "image"


@pytest.mark.parametrize("parent", [None, "bundle.zip"])
def test_the_prompt_names_the_file_the_way_a_source_will_cite_it(parent: str | None):
    document = Document(
        filename="Req.xlsx", kind=FileKind.XLSX, text="items", parent=parent
    )

    assert document.origin in prompt(document)


def test_the_prompt_tells_the_model_the_sample_was_cut_short():
    """Otherwise a truncated file looks to the model like an empty one."""
    document = DocumentLoader(Budget(max_spreadsheet_rows=10)).load(
        [SourceFile(filename="big.csv", data=b"a,b\n" + b"1,2\n" * 50, size_bytes=200)]
    )[0]

    assert "rows_truncated" in prompt(document)


def test_a_real_spreadsheet_reads_back_the_rows_it_holds():
    document = DocumentLoader(Budget()).load(
        [SourceFile(filename="Requisition.xlsx", data=build.xlsx_requisition(), size_bytes=5000)]
    )[0]

    assert "ROPE PP 24MM X 220M" in prompt(document)
