"""B3, the half without a model: finding the table and measuring its columns.

Pure functions over a grid, so every case here is an exact assertion. This is
the part that has to be right before the model is asked anything - a wrong
header row makes every answer wrong, and a profile is the only thing that shows
a column filled in seven rows out of two hundred.
"""

from src.infrastructure.documents.models import Grid
from src.services.extraction.table import (
    MAX_HEADER_SCAN_ROWS,
    SAMPLES_PER_COLUMN,
    block_at,
    column_letter,
    find_blocks,
    is_numeric,
)

HEADER = ["ITEM", "IMPA", "DESCRIPTION", "QTY", "UNIT"]


def grid(rows: list[list[str | None]], name: str = "Sheet1") -> Grid:
    return Grid(origin=f"Req.xlsx#{name}", rows=rows, name=name)


def item(number: int, remark: str | None = None) -> list[str | None]:
    return [str(number), f"55010{number % 10}", f"ROPE {number}", str(number), "coil", remark]


# --- finding the header row -----------------------------------------------


def test_the_header_is_found_under_a_title_row():
    """Real exports put a title and a blank line above the table."""
    found = find_blocks(grid([["REQUISITION 78432"], [], HEADER, item(1), item(2)]))

    assert len(found) == 1
    assert found[0].header_row == 2
    assert found[0].first_data_row == 3
    assert found[0].height == 2


def test_the_title_above_the_header_is_kept():
    found = find_blocks(grid([["DECK STORES"], HEADER, item(1)]))

    assert found[0].title == "DECK STORES"


def test_a_single_matching_word_is_not_a_header():
    """A data row saying "item" must not be mistaken for column headings."""
    assert find_blocks(grid([["item"], ["one"], ["two"]])) == []


def test_a_grid_with_no_header_yields_no_block():
    assert find_blocks(grid([["a", "b"], ["1", "2"]])) == []


def test_a_header_with_no_rows_under_it_is_not_a_block():
    assert find_blocks(grid([HEADER])) == []


def test_trailing_blank_rows_are_not_data():
    found = find_blocks(grid([HEADER, item(1), [], [None, None]]))

    assert found[0].height == 1


def test_a_header_far_down_a_sheet_is_ignored():
    """Past thirty rows a match is data that happens to say "item"."""
    rows = [["filler"] for _ in range(MAX_HEADER_SCAN_ROWS + 2)]
    rows += [HEADER, item(1)]

    assert find_blocks(grid(rows)) == []


# --- several tables in one sheet ------------------------------------------


def test_two_stacked_tables_are_both_found():
    """"DECK STORES" then "ENGINE STORES" - reading only the first loses half."""
    found = find_blocks(
        grid(
            [
                ["DECK STORES"], HEADER, item(1), item(2), [],
                ["ENGINE STORES"], HEADER, item(3),
            ]
        )
    )

    assert [block.title for block in found] == ["DECK STORES", "ENGINE STORES"]
    assert [block.height for block in found] == [2, 1]
    # The blank line and the next table's title are trailing noise, not items.


def test_a_second_table_is_found_even_below_the_scan_window():
    """The window bounds the first header only; a repeat further down is real."""
    rows = [HEADER] + [item(number) for number in range(MAX_HEADER_SCAN_ROWS + 5)]
    rows += [HEADER, item(99)]

    found = find_blocks(grid(rows))

    assert len(found) == 2
    assert found[1].height == 1


def test_the_first_block_stops_where_the_second_begins():
    found = find_blocks(grid([HEADER, item(1), item(2), HEADER, item(3)]))

    assert found[0].height == 2
    assert found[1].height == 1


# --- column profiles ------------------------------------------------------


def test_a_profile_counts_every_row_not_the_sample():
    block = find_blocks(grid([HEADER, *[item(n) for n in range(1, 101)]]))[0]
    quantity = block.profile_for("D")

    assert quantity.total == 100
    assert quantity.filled == 100
    assert quantity.numeric_share == 1.0


def test_a_rarely_filled_column_is_visible_in_the_profile():
    """The column three sample rows would never show."""
    rows = [item(number) for number in range(1, 101)]
    rows[46][5] = "see drawing"
    block = find_blocks(grid([[*HEADER, "REMARKS"], *rows]))[0]

    remarks = block.profile_for("F")
    assert remarks.filled == 1
    assert remarks.is_rare
    assert remarks.samples == ["see drawing"]


def test_an_empty_column_is_reported_as_empty_not_as_text():
    block = find_blocks(grid([[*HEADER, "PRICE"], item(1), item(2)]))[0]

    assert block.profile_for("F").is_empty
    assert "empty" in block.profile_for("F").render()


def test_a_text_column_is_not_called_numeric():
    block = find_blocks(grid([HEADER, item(1), item(2)]))[0]

    assert block.profile_for("C").numeric_share == 0.0
    assert "text" in block.profile_for("C").render()


def test_a_quantity_written_with_its_unit_still_counts_as_numeric():
    """Customers write "2" and "2 coil" in the same column."""
    assert is_numeric("2")
    assert is_numeric("2 coil")
    assert is_numeric("1,5")
    assert not is_numeric("ROPE PP 24MM")
    assert not is_numeric(None)


# --- the sample sent to the model -----------------------------------------


def test_a_small_table_is_sent_whole():
    block = find_blocks(grid([HEADER, item(1), item(2), item(3)]))[0]

    assert len(block.sample_rows()) == 3


def test_a_large_table_is_sampled_from_the_top_middle_and_end():
    block = find_blocks(grid([HEADER, *[item(n) for n in range(1, 101)]]))[0]
    positions = [number for number, _ in block.sample_rows()]

    assert len(positions) >= 3 * SAMPLES_PER_COLUMN
    assert positions == sorted(positions), "the sample keeps the customer's order"
    assert positions[0] == 1, "the first data row is always shown"
    assert positions[-1] == 100, "so is the last"


def test_the_sample_always_includes_a_row_that_fills_a_rare_column():
    """Otherwise the model is asked about a column it cannot see."""
    rows = [item(number) for number in range(1, 101)]
    rows[46][5] = "see drawing"
    block = find_blocks(grid([[*HEADER, "REMARKS"], *rows]))[0]

    shown = [cells for _, cells in block.sample_rows()]
    assert any(cells[5] == "see drawing" for cells in shown)


# --- taking the model's word on the header row ----------------------------


def test_a_row_the_word_list_missed_can_still_be_taken_as_the_header():
    """The model looked at the rows; the word list only matched strings."""
    rows = [["Pos", "Artikel", "Menge"], ["1", "TAU", "2"], ["2", "FARBE", "5"]]
    block = block_at(grid(rows), 0)

    assert block is not None
    assert block.height == 2
    assert block.profile_for("B").header == "Artikel"


def test_a_header_row_outside_the_grid_is_refused():
    assert block_at(grid([HEADER, item(1)]), 9) is None
    assert block_at(grid([HEADER, item(1)]), -1) is None


# --- column letters -------------------------------------------------------


def test_column_letters_match_what_excel_shows():
    assert [column_letter(index) for index in (0, 1, 25, 26, 27, 51)] == [
        "A", "B", "Z", "AA", "AB", "AZ"
    ]


# --- shapes found in real customer workbooks ------------------------------


def test_dropdown_lists_parked_beside_a_table_do_not_stretch_it():
    """Measured on a real ALMI requisition: two items, and 706 rows of UOM
    names parked in columns A-F. Measuring the table by "the last row with
    anything in it" buried the two items in reference data."""
    rows = [["BAG 1 KG", None, "VESSEL:", "ALMI HYDRA"]]
    rows += [["BAG 2 KG", None, None, None] for _ in range(6)]
    rows.append(["BAG 16 KG", None, "ITEM CODE", "QUANTITY"])
    rows.append(["BAG 17 KG", None, "NI55910", "170"])
    rows += [[f"BAG {n} KG", None, None, None] for n in range(18, 300)]

    block = find_blocks(grid(rows))[0]

    assert block.header_row == 7
    assert block.height == 1, "the list runs on for 300 rows; the table does not"
    assert [column.letter for column in block.profiles] == ["C", "D"]


def test_a_heading_that_is_really_list_data_is_not_taken_as_a_column():
    """"BAG 16 KG" sits in the header row and looks just as much like a heading
    as "ITEM CODE" beside it. Position is what separates them."""
    rows = [
        ["BAG 16 KG", "BOTTLE 1 Gal", None, "No", "ITEM CODE", "QUANTITY"],
        ["BAG 17 KG", "BOTTLE 1.5 LT", None, "1", "NI55910", "170"],
    ]
    block = find_blocks(grid(rows))[0]

    assert [column.letter for column in block.profiles] == ["D", "E", "F"]
