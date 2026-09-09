"""Stage C at the file level: values go in, and nothing else moves.

The claim this stage makes is unusually strong - a filled copy differs from the
master in the cells that were filled and nowhere else - and it is testable
exactly because the writer edits bytes instead of rebuilding the workbook. Most
of what follows is that comparison, in one shape or another.
"""

import hashlib
import io
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest

from src.infrastructure.excel import (
    BLANK,
    Day,
    Moment,
    Number,
    Template,
    TemplateShapeError,
    Text,
    excel_serial,
    excel_timestamp,
)
from src.infrastructure.excel.sheet import column_letters
from src.infrastructure.excel.styles import StyleTable
from tests.workbook_builder import LAST_ROW, PARTS, VBA, master, with_sheet

# The form the service ships and fills. Small, blank, and in the repository.
FORM = Path("config/rfq_output_template.xlsx")

FIRST_ITEM_ROW = 24


@pytest.fixture(scope="module")
def template() -> Template:
    return Template(master())


def sheet(data: bytes):
    """The filled workbook as a spreadsheet reader sees it.

    Read by openpyxl rather than by looking at the XML: the point of most of
    these tests is that a real reader agrees with us about what we wrote.
    """
    # Without `keep_vba`: openpyxl would hold a second handle on the archive and
    # then read from it after closing it. The macro project is checked as bytes.
    book = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    page = book["KASS RFQ Template"]
    book.close()
    return page

def part(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        return book.read(name)


# --- what goes in ---------------------------------------------------------


def test_text_lands_in_the_cell_it_was_addressed_to(template: Template):
    cells = sheet(template.fill({"C2": Text("MV ALMI GLOBE")}))

    assert cells["C2"].value == "MV ALMI GLOBE"


def test_text_goes_in_inline_so_the_shared_strings_stay_untouched(template: Template):
    """A value added to `sharedStrings.xml` would renumber every string in the
    workbook. Inline strings cost a few bytes and leave the part alone."""
    filled = template.fill({"C2": Text("MV ALMI GLOBE")})

    assert b"inlineStr" in part(filled, "xl/worksheets/sheet1.xml")
    assert part(filled, "xl/sharedStrings.xml") == PARTS["xl/sharedStrings.xml"].encode()


def test_a_number_goes_in_as_a_number(template: Template):
    cells = sheet(template.fill({"E24": Number(12)}))

    assert cells["E24"].value == 12
    assert not isinstance(cells["E24"].value, str)


def test_a_whole_number_does_not_grow_a_decimal_point(template: Template):
    assert b"<v>12</v>" in part(template.fill({"E24": Number(12.0)}), "xl/worksheets/sheet1.xml")


def test_a_date_becomes_a_date_and_not_a_five_digit_number(template: Template):
    cells = sheet(template.fill({"H3": Day(date(2026, 10, 12))}))

    assert cells["H3"].value == datetime(2026, 10, 12)


def test_a_date_cell_keeps_the_format_the_master_gave_it(template: Template):
    """`H3` already shows dates the way the mapping document asks for. Nothing
    should improve on it."""
    cells = sheet(template.fill({"H3": Day(date(2026, 10, 12))}))

    assert cells["H3"].number_format == "[$-409]d\\-mmm\\-yyyy"


def test_a_date_in_a_general_cell_is_given_a_format_to_be_read_by(template: Template):
    """`H5` is General in the master, because a person typing into it lets Excel
    apply a format on the way in. Nobody types into ours."""
    cells = sheet(template.fill({"H5": Day(date(2026, 10, 8))}))

    assert cells["H5"].value == datetime(2026, 10, 8)
    assert cells["H5"].number_format != "General"


def test_giving_one_cell_a_date_format_does_not_give_its_neighbour_one(template: Template):
    """`H5` and `H7` share a style, and one of them holds an address."""
    cells = sheet(template.fill({"H5": Day(date(2026, 10, 8)), "H7": Text("Anchorage B")}))

    assert cells["H7"].value == "Anchorage B"
    assert cells["H7"].number_format == "General"


def test_a_timestamp_keeps_its_time(template: Template):
    cells = sheet(template.fill({"H12": Moment(datetime(2026, 9, 5, 8, 14))}))

    assert cells["H12"].value == datetime(2026, 9, 5, 8, 14)


def test_a_blank_clears_the_demo_value_the_master_ships_with(template: Template):
    """The master arrives with `AED` in the currency cell. A copy that keeps it
    because nobody found a currency has invented one."""
    assert sheet(master())["H8"].value == "AED"

    assert sheet(template.fill({"H8": BLANK}))["H8"].value is None


def test_a_cell_the_row_does_not_have_is_inserted_where_it_belongs(template: Template):
    """The master's item rows have no column C at all. Cells have to stay in
    column order or Excel calls the file corrupt."""
    filled = template.fill({"C24": Text("inserted"), "D24": Text("described")})
    cells = sheet(filled)
    row = part(filled, "xl/worksheets/sheet1.xml").split(b'<row r="24">')[1]

    assert cells["C24"].value == "inserted"
    assert row.index(b'r="C24"') < row.index(b'r="D24"')


def test_the_customers_own_punctuation_survives_the_trip(template: Template):
    written = 'ROPE 24MM <"heavy duty"> & spare'

    assert sheet(template.fill({"D24": Text(written)}))["D24"].value == written


def test_a_control_character_is_dropped_rather_than_breaking_the_file(template: Template):
    """XML 1.0 cannot encode these, and a NUL out of a badly written CSV is not
    worth losing an RFQ over."""
    cells = sheet(template.fill({"D24": Text("ROPE\x00 24MM\x0c")}))

    assert cells["D24"].value == "ROPE 24MM"


def test_text_longer_than_excel_accepts_is_cut_rather_than_refused(template: Template):
    cells = sheet(template.fill({"D24": Text("x" * 40_000)}))

    assert len(cells["D24"].value) == 32_767


def test_many_rows_go_in_in_the_order_they_were_given(template: Template):
    rows = {f"A{FIRST_ITEM_ROW + n}": Number(n + 1) for n in range(10)}
    rows |= {f"D{FIRST_ITEM_ROW + n}": Text(f"item {n + 1}") for n in range(10)}

    cells = sheet(template.fill(rows))

    assert [cells[f"A{FIRST_ITEM_ROW + n}"].value for n in range(10)] == list(range(1, 11))
    assert cells["D33"].value == "item 10"


# --- what must not move ---------------------------------------------------


def test_the_formula_cells_are_left_exactly_as_they_were(template: Template):
    """`A1` looks the customer up from the sender code and `H10` restates `H12`.
    The task says not to touch them, and byte equality is how we know."""
    original = part(master(), "xl/worksheets/sheet1.xml")
    filled = part(template.fill({"C2": Text("MV ALMI GLOBE")}), "xl/worksheets/sheet1.xml")

    assert original.split(b"</row>")[0] == filled.split(b"</row>")[0]


def test_the_macro_project_is_copied_and_not_rebuilt(template: Template):
    """The workbook's own macro normalizes units of measure. A library that
    re-serializes the book gives no promise about this part."""
    assert part(template.fill({"C2": Text("X")}), "xl/vbaProject.bin") == VBA


def test_only_the_three_parts_we_have_a_reason_to_touch_change(template: Template):
    filled = template.fill({"C2": Text("MV ALMI GLOBE"), "H5": Day(date(2026, 10, 8))})
    original = master()

    changed = {name for name in PARTS if part(original, name) != part(filled, name)}

    assert changed == {"xl/worksheets/sheet1.xml", "xl/styles.xml", "xl/workbook.xml"}


def test_the_styles_are_left_alone_when_no_new_format_is_needed(template: Template):
    """Only a date landing in a cell that cannot show one costs a style."""
    filled = template.fill({"C2": Text("X"), "H3": Day(date(2026, 10, 12))})

    assert part(filled, "xl/styles.xml") == PARTS["xl/styles.xml"].encode()


def test_the_parts_keep_their_names_and_their_order(template: Template):
    with zipfile.ZipFile(io.BytesIO(master())) as before, \
         zipfile.ZipFile(io.BytesIO(template.fill({"C2": Text("X")}))) as after:
        assert before.namelist() == after.namelist()


def test_the_copy_recalculates_its_formulas_when_it_opens(template: Template):
    """Excel shows the value it last calculated. `H10` is derived from a cell we
    write, so the copy has to be told the cached one is stale."""
    assert b'<calcPr fullCalcOnLoad="1"/>' in part(template.fill({}), "xl/workbook.xml")


def test_filling_nothing_leaves_the_sheet_byte_for_byte(template: Template):
    assert part(template.fill({}), "xl/worksheets/sheet1.xml") == PARTS[
        "xl/worksheets/sheet1.xml"
    ].encode()


# --- what it refuses to do ------------------------------------------------


def test_an_item_list_longer_than_the_sheet_extends_it(template: Template):
    """The output template is laid out to row 177 and a real RFQ arrived with
    211 items. The rows past the end are added rather than lost."""
    filled = template.fill({f"A{LAST_ROW + n}": Number(n) for n in range(1, 4)})
    cells = sheet(filled)

    assert [cells[f"A{LAST_ROW + n}"].value for n in range(1, 4)] == [1, 2, 3]
    assert cells.max_row == LAST_ROW + 3


def test_a_gap_in_the_middle_of_the_sheet_is_an_error_not_a_lost_item(template: Template):
    """A row written out of order is a file Excel calls corrupt, so nothing is
    written at all."""
    missing = re.sub(r'<row r="20">.*?</row>', "", PARTS["xl/worksheets/sheet1.xml"], flags=re.S)
    patched = Template(with_sheet(missing))

    with pytest.raises(TemplateShapeError):
        patched.fill({"A20": Number(1)})


def test_something_that_is_not_a_cell_reference_is_refused(template: Template):
    with pytest.raises(ValueError):
        template.fill({"not a cell": Text("x")})


def test_a_workbook_missing_the_parts_we_write_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("xl/workbook.xml", "<workbook/>")

    with pytest.raises(TemplateShapeError):
        Template(buffer.getvalue())


# --- how a date is stored -------------------------------------------------


def test_excel_counts_days_from_an_epoch_of_its_own():
    """The format counts a 29 February 1900 that never happened, which is why
    the epoch is 30 December 1899 rather than the first of January."""
    assert excel_serial(date(1900, 3, 1)) == 61
    assert excel_serial(date(2026, 10, 12)) == 46307


def test_a_time_of_day_is_the_fraction_of_the_day_that_has_passed():
    assert excel_timestamp(datetime(2026, 10, 12, 6, 0)) == 46307.25


# --- choosing a date format -----------------------------------------------


def test_a_style_that_already_shows_a_date_is_reused_rather_than_cloned():
    styles = StyleTable(PARTS["xl/styles.xml"].encode())

    assert styles.for_date(2, with_time=False) == 2
    assert styles.to_xml() == PARTS["xl/styles.xml"].encode()


def test_one_variant_is_made_per_style_however_many_cells_need_it():
    styles = StyleTable(PARTS["xl/styles.xml"].encode())

    first = styles.for_date(1, with_time=False)
    second = styles.for_date(1, with_time=False)

    assert first == second != 1
    assert b'<cellXfs count="5">' in styles.to_xml()


def test_the_variant_borrows_a_format_the_workbook_already_declares():
    """`d-mmm-yyyy` is the shape the mapping document asks for, and the master
    already has it. Imposing `mm-dd-yy` from outside would be worse."""
    styles = StyleTable(PARTS["xl/styles.xml"].encode())

    styles.for_date(1, with_time=False)

    assert b'numFmtId="164"' in styles.to_xml().split(b"<cellXfs")[1]


# --- the form the desk actually receives -----------------------------------


@pytest.fixture(scope="module")
def real() -> Template:
    return Template.load(FORM)


@pytest.fixture(scope="module")
def filled_real(real: Template) -> bytes:
    """The shipped template, filled the way the service fills it.

    This is the file a person opens, and it is 14 KB rather than the 12.5 MB
    master it was cut from - which is why every test below can afford to use it.
    """
    return real.fill(
        {
            "C2": Text("DHT ANTELOPE"),
            "C3": Number(1055179),
            "C4": Text("DANT260189"),
            "C5": Text("SINGAPORE"),
            "C10": Text("REQUEST FOR QUOTE"),
            "H2": Text("SINGAPORE - SINGAPORE"),
            "H8": Text("SGD"),
            "H9": Text("GM8620"),
            "H10": Moment(datetime(2026, 9, 7, 8, 33)),
            "H11": Text("ENGINE"),
            "H12": Moment(datetime(2026, 9, 7, 8, 33)),
            "A24": Number(1),
            "B24": Number(696737),
            "D24": Text("U-BOLT STEEL PIPE 65A M12, WITH SEAT & NUT - "),
            "E24": Number(6),
            "F24": Text("SET"),
        }
    )


def test_the_shipped_form_arrives_blank(real: Template):
    """Everything the agent fills is empty in it, and the labels are not."""
    cells = sheet(real.fill({}))

    assert cells["A2"].value == "VESSEL NAME*:"
    assert cells["D23"].value == "ITEM DESCRIPTION"
    assert [cells[ref].value for ref in ("C2", "C5", "H2", "H8", "H9", "H11", "D24")] == [
        None
    ] * 7


def test_the_shipped_form_carries_no_customer_data(real: Template):
    """It was made by clearing somebody's real RFQ, and clearing a cell leaves
    its text in `sharedStrings.xml` - invisible in Excel, plain in the file.
    This one is in version control, so the strings are pruned as well."""
    everything = FORM.read_bytes()

    for trace in (b"DHT ANTELOPE", b"GM8620", b"DANT260189", b"U-BOLT", b"SGD"):
        assert trace not in everything, f"{trace!r} survived in the shipped template"


def test_the_form_takes_the_values_where_they_were_meant_to_go(filled_real: bytes):
    cells = sheet(filled_real)

    assert cells["C2"].value == "DHT ANTELOPE"
    assert cells["C3"].value == 1055179
    assert cells["H2"].value == "SINGAPORE - SINGAPORE"
    assert cells["H9"].value == "GM8620"
    assert cells["H10"].value == datetime(2026, 9, 7, 8, 33)
    assert cells["D24"].value == "U-BOLT STEEL PIPE 65A M12, WITH SEAT & NUT - "


def test_the_form_keeps_its_labels_and_its_layout(filled_real: bytes):
    """Thirty-six merged ranges and a drawing. A library that rebuilt the
    workbook would have to promise all of it; the patch does not have to."""
    book = openpyxl.load_workbook(io.BytesIO(filled_real))
    page = book["KASS RFQ Template"]
    merges = {str(span) for span in page.merged_cells.ranges}
    book.close()

    assert len(merges) == 36
    assert "A1:J1" in merges and "A14:J21" in merges
    assert part(filled_real, "xl/drawings/drawing1.xml") == part(FORM.read_bytes(), "xl/drawings/drawing1.xml")


def test_the_form_changes_in_one_part_and_no_others(filled_real: bytes):
    """One part out of twelve. No date lands in a cell that cannot show one, so
    the styles do not move either, and the form was made through the same code
    so its workbook part already says to recalculate."""
    with zipfile.ZipFile(FORM) as before, zipfile.ZipFile(io.BytesIO(filled_real)) as after:
        changed = {
            name for name in before.namelist() if before.read(name) != after.read(name)
        }

    assert changed == {"xl/worksheets/sheet1.xml"}


def test_the_checksum_names_the_form_the_copy_was_cut_from(real: Template):
    assert real.checksum == hashlib.sha256(FORM.read_bytes()).hexdigest()
