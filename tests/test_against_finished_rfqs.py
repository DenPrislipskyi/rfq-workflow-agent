"""The agent's form against the two the desk filled in by hand.

The strongest check available: not "does a value reach a cell" but "does the
file we produce match the file a person produced from the same RFQ". Both
comparisons are cell by cell.

Two cells are left out of the comparison on purpose. Their `H2` reads
`SINGAPORE - SINGAPORE` where the email said `Singapore`, and their `H9` holds a
code that appears in no email at all: both come from looking values up in the
master workbook, which this build does not do. That lookup is in
`archive/master_matching/`.

Skipped when the customer's own files are not in the checkout - `docs/` is
ignored by git on purpose, because these are real RFQs for real vessels.
"""

import asyncio
import io
from datetime import UTC, datetime
from pathlib import Path

import openpyxl
import pytest

from src.infrastructure.excel import Template
from src.services.extraction import RfqExtraction
from src.services.extraction.models import HeaderField, HeaderValue, LineItem, RfqHeader
from src.services.extraction.normalize import normalize_header
from src.services.workbook import FIRST_ITEM_ROW, WorkbookBuilder

FORM_PATH = Path("config/rfq_output_template.xlsx")

# What a perfect reader would have got out of each email, so that what is under
# test is the mapping and the writing rather than the model.
CASES = {
    "docs/DANT260189 1 (1).xlsx": (
        datetime(2026, 9, 7, 8, 33, tzinfo=UTC),
        "Goodwood Ship Management",
        {
            HeaderField.VESSEL_NAME: "DHT ANTELOPE",
            HeaderField.IMO: "1055179",
            HeaderField.RFQ_REFERENCE: "DANT260189",
            HeaderField.DELIVERY_PORT: "SINGAPORE - SINGAPORE",
            HeaderField.CURRENCY: "SGD",
            HeaderField.RFQ_TYPE: "ENGINE",
        },
    ),
    "docs/PR PM 26-27 01998 1.xlsx": (
        datetime(2026, 9, 5, 8, 33, tzinfo=UTC),
        None,
        {
            HeaderField.VESSEL_NAME: "GLEN COVE",
            HeaderField.IMO: "8991619",
            HeaderField.RFQ_REFERENCE: "PR/PM/26-27/01998",
            HeaderField.DELIVERY_PORT: "SINGAPORE - SINGAPORE",
            HeaderField.CURRENCY: "SGD",
            HeaderField.RFQ_TYPE: "PROVISION",
        },
    ),
}

BRANCH = "SINGAPORE"
# Everything but the two cells that come out of the master workbook.
COMPARED = ("C2", "C3", "C4", "C5", "C10", "H8", "H10", "H11", "H12")


def open_case(name: str):
    for path in (Path(name), FORM_PATH):
        if not path.is_file():
            pytest.skip(f"{path} is not in the checkout")
    return openpyxl.load_workbook(name).worksheets[0]


def items_of(page) -> list[LineItem]:
    """The item rows as a reader would have handed them over: verbatim strings."""
    found: list[LineItem] = []
    for row in page.iter_rows(min_row=FIRST_ITEM_ROW, max_row=page.max_row, max_col=6):
        cells = {cell.column_letter: cell.value for cell in row if cell.value is not None}
        if not cells.get("D"):
            continue
        found.append(
            LineItem(
                sr_no=len(found) + 1,
                customer_item_code=str(cells["B"]) if cells.get("B") else None,
                description=str(cells["D"]),
                quantity=str(cells["E"]) if cells.get("E") is not None else None,
                uom=str(cells["F"]) if cells.get("F") else None,
                source=Path("requisition").name,
            )
        )
    return found


def ours_for(name: str):
    """The form the agent would produce for that RFQ."""
    theirs = open_case(name)
    received, company, fields = CASES[name]

    header = RfqHeader(
        fields={
            name: HeaderValue(value=value, source="email.body")
            for name, value in fields.items()
        },
        customer_company=(
            HeaderValue(value=company, source="email.body") if company else None
        ),
    )
    extraction = RfqExtraction(
        header=header,
        normalized=normalize_header(header, today=received.date()),
        items=items_of(theirs),
    )
    filled = asyncio.run(
        WorkbookBuilder(Template.load(FORM_PATH)).build(
            extraction, branch=BRANCH, received_at=received
        )
    )
    return theirs, openpyxl.load_workbook(io.BytesIO(filled.data)).worksheets[0], filled


@pytest.mark.parametrize("name", CASES)
def test_every_header_cell_matches_the_one_a_person_filled(name: str):
    theirs, ours, _ = ours_for(name)

    assert {ref: ours[ref].value for ref in COMPARED} == {
        ref: theirs[ref].value for ref in COMPARED
    }


@pytest.mark.parametrize("name", CASES)
def test_every_item_cell_matches_including_the_two_hundred_and_eleventh(name: str):
    """One of these RFQs is 211 lines of provisions with the customer's own
    typos in it. The item grid is 1,055 cells and all of them are compared."""
    theirs, ours, filled = ours_for(name)
    columns = ("A", "B", "D", "E", "F")

    rows = range(FIRST_ITEM_ROW, FIRST_ITEM_ROW + filled.items)
    assert {f"{c}{r}": ours[f"{c}{r}"].value for r in rows for c in columns} == {
        f"{c}{r}": theirs[f"{c}{r}"].value for r in rows for c in columns
    }


@pytest.mark.parametrize("name", CASES)
def test_the_columns_a_later_step_owns_are_empty_in_both(name: str):
    theirs, ours, filled = ours_for(name)

    for row in range(FIRST_ITEM_ROW, FIRST_ITEM_ROW + filled.items):
        for column in ("C", "G", "H", "I", "J"):
            assert ours[f"{column}{row}"].value is None
            assert theirs[f"{column}{row}"].value is None


@pytest.mark.parametrize("name", CASES)
def test_the_file_is_named_the_way_the_desk_names_it(name: str):
    _, _, filled = ours_for(name)

    assert filled.filename == Path(name).name.replace(" 1 (1)", "").replace(" 1", "")


def test_the_two_cells_that_need_the_master_workbook():
    """`H2` and `H9`, and both are honest gaps rather than defects.

    Their `H2` reads `SINGAPORE - SINGAPORE` because a person looked the port up
    in the branch's list; ours carries whatever the email said. Their `H9` is
    `GM8620`, a code that appears in no email anywhere - it exists only in the
    master workbook, so without that lookup the cell stays empty.

    Pinned here so the gap is visible rather than forgotten. The lookup that
    closes it is in `archive/master_matching/`.
    """
    theirs, ours, _ = ours_for("docs/DANT260189 1 (1).xlsx")

    assert theirs["H9"].value == "GM8620"
    assert ours["H9"].value is None
