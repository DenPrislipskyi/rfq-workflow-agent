"""Matching a customer's wording against our own product list.

The cases here are the ones the desk's own mapping notes single out, in the
desk's own words: a code that contradicts the description, a line with no code
at all, and two products that differ by one number in the middle of an
otherwise identical sentence.
"""

from src.domain.rules.catalog import Catalog, CatalogItem, normalize_code, tokenize

# Two columns per row, and the difference between them is the point. Ours is
# what the table shows for a product; the customer's is what the search reads,
# because a line of an RFQ is written by a customer.
ROWS = [
    {"Item Code": "T69128400", "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM",
     "Customer Description": "Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65"},
    {"Item Code": "T69133100", "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M20 X 80MM",
     "Customer Description": "Hexagon Head Bolts Full Threaded (Bolt with Nut) M20*80"},
    {"Item Code": "T69114500", "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M8 X 50MM",
     "Customer Description": "Hexagon Head Bolts Full Threaded (Bolt with Nut) M8*50"},
    {"Item Code": "T85111100", "Item Description": "GOGGLE WELDER METAL FLIP-UP, 45MM LENS DIAM",
     "Customer Description": "WELDING GOGGLES"},
    {"Item Code": "T85116300", "Item Description": "WELDER GLOVES FIVE FINGERS",
     "Customer Description": "Weldings gloves(five fingers)"},
    {"Item Code": "T65082300", "Item Description": "RULE CONVEX STEEL METRIC 5MTR",
     "Customer Description": "Convex rulers steel 5m"},
    {"Item Code": "T11018800", "Item Description": "ROD FISHING WITH FURTHER, DETAILS",
     "Customer Description": "EXTERNAL HDD 4TB"},
]


def catalog(rows=ROWS) -> Catalog:
    return Catalog.from_rows(
        rows,
        code_column="Item Code",
        description_column="Item Description",
        customer_description_column="Customer Description",
    )


# --- what gets into the catalogue at all ---------------------------------


def test_a_row_without_a_code_or_a_description_is_not_an_item():
    """Neither half can be guessed, and an item nothing can match is not one."""
    built = catalog(
        [
            *ROWS,
            {"Item Code": "T00000000", "Item Description": ""},
            {"Item Code": "", "Item Description": "SOMETHING WITH NO CODE"},
        ]
    )

    assert len(built) == len(ROWS)


def test_headings_are_matched_the_way_people_retype_them():
    built = Catalog.from_rows(
        [{"ITEM  CODE": "T1", "item_description": "WIRE ROPE 12MM"}],
        code_column="Item Code",
        description_column="Item Description",
    )

    assert built.by_code("T1") is not None


def test_the_whole_row_is_kept_even_though_two_columns_are_used():
    """The sheet grows columns - UOM, pack size, stock - and this module has no
    business deciding in advance which of them will matter."""
    built = Catalog.from_rows(
        [{"Item Code": "T1", "Item Description": "WIRE ROPE 12MM", "UOM": "MTR"}],
        code_column="Item Code",
        description_column="Item Description",
    )

    assert built.items[0].fields["UOM"] == "MTR"


# --- by code --------------------------------------------------------------


def test_a_code_is_found_however_the_customer_punctuated_it():
    found = catalog().by_code(" t-691284/00 ")

    assert found is not None and found.code == "T69128400"


def test_a_code_nobody_has_is_not_a_match():
    assert catalog().by_code("T99999999") is None
    assert catalog().by_code(None) is None
    assert catalog().by_code("") is None


# --- by description -------------------------------------------------------


def test_the_right_bolt_of_three_nearly_identical_ones():
    """Six of seven words are shared; the size is the whole answer."""
    found = catalog().search("Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65")

    assert found[0].item.code == "T69128400"


def test_words_the_customer_did_not_use_do_not_win():
    found = catalog().search("welding goggles")

    assert found[0].item.code == "T85111100"


def test_a_query_that_normalizes_to_nothing_matches_nothing():
    """The failure mode this project has already had once: a key that empties
    out under normalization is contained in every string there is."""
    assert catalog().search("   ") == []
    assert catalog().search("...") == []
    assert catalog().search("") == []


def test_a_description_in_another_script_survives_normalization():
    built = catalog(
        [
            {
                "Item Code": "T1",
                "Item Description": "ROPE",
                "Customer Description": "(주)씨웨이글로벌 ROPE",
            }
        ]
    )

    assert tokenize("(주)씨웨이글로벌") == ["주", "씨웨이글로벌"]
    assert built.search("씨웨이글로벌")[0].item.code == "T1"


def test_one_candidate_per_product_however_many_rows_it_has():
    """A row of the sheet is a case of a mapping, not a product. Five ways of
    writing one boilersuit is one option, not five."""
    built = catalog(
        [
            {"Item Code": "T31237400", "Item Description": "BOILERSUIT NAVY 3XL",
             "Customer Description": "boilersuit navy 3XL"},
            {"Item Code": "T31237400", "Item Description": "BOILERSUIT NAVY 2XL",
             "Customer Description": "boilersuit navy 2XL"},
            {"Item Code": "T19036300", "Item Description": "SNEAKERS STEEL TOE 25CM",
             "Customer Description": "sneakers steel toe 25cm"},
        ]
    )

    found = built.search("boilersuit navy", limit=5)

    assert [candidate.item.code for candidate in found] == ["T31237400"]


def test_a_repeated_code_resolves_to_the_first_row():
    """The sheet is meant to hold each code once, so a repeat is a data error
    rather than a choice. Taking the first is at least repeatable."""
    built = catalog(
        [
            {"Item Code": "T1", "Item Description": "FIRST"},
            {"Item Code": "T1", "Item Description": "SECOND"},
        ]
    )

    found = built.by_code("T1")

    assert found is not None and found.description == "FIRST"


def _with_wording(*, indexed: bool) -> Catalog:
    """One product our own words and the customer's share nothing at all."""
    return Catalog.from_rows(
        [
            {
                "Item Code": "T55029103",
                "Item Description": "HAND WASH LIQUID DETTOL 200 ML WITH PUMP BOTTLE",
                "Customer Description": "sanitiser gel",
            }
        ],
        code_column="Item Code",
        description_column="Item Description",
        customer_description_column="Customer Description",
        index_item_description=indexed,
    )


def test_the_customer_s_wording_is_what_the_search_reads():
    """A line of an RFQ is written by a customer, so what it resembles is how
    a customer asked for the same thing before."""
    assert _with_wording(indexed=False).search("sanitiser gel")[0].item.code == "T55029103"
    assert _with_wording(indexed=False).items[0].customer_description == "sanitiser gel"


def test_our_own_wording_is_searched_only_when_that_is_switched_on():
    """Off by default: the desk's specification is that a line is matched
    against the sheet's customer wording column, and that column alone."""
    assert _with_wording(indexed=False).search("hand wash dettol pump bottle") == []
    assert _with_wording(indexed=True).search("hand wash dettol pump bottle")[0].item.code == (
        "T55029103"
    )


def test_the_shortlist_is_as_long_as_it_was_asked_to_be():
    assert len(catalog().search("hexagon head bolts", limit=2)) == 2


def test_our_code_pasted_into_the_description_still_finds_the_item():
    """Customers put our code in the text as often as in a column of its own."""
    found = catalog().search("please quote T69133100")

    assert found[0].item.code == "T69133100"


# --- the two together -----------------------------------------------------


def test_the_code_leads_the_shortlist_and_the_words_confirm_it():
    shortlist = catalog().shortlist(
        code="T69128400", description="Hexagon Head Bolts (Bolt with Nut) M16*65"
    )

    assert shortlist.by_code is not None and shortlist.by_code.code == "T69128400"
    assert shortlist.candidates[0].item.code == "T69128400"
    assert shortlist.confirmed is True
    assert shortlist.conflicted is False


def test_a_code_whose_customer_wording_says_something_else_is_flagged():
    """A code is confirmed by the wording the sheet files it under, so what
    contradicts it is a line that wording does not answer."""
    shortlist = catalog().shortlist(code="T69128400", description="Convex rulers steel 5m")

    assert shortlist.by_code is not None
    assert shortlist.confirmed is False
    assert shortlist.conflicted is True


def test_the_desks_negative_mapping_is_no_longer_visible_here():
    """Row 110188 is the desk's own example of a mapping that went wrong: the
    customer asked for a hard drive and the item the code names is a fishing
    rod. Searching the customer wording column cannot see that, because that
    column is the customer's request - it agrees with the customer by
    construction. Matching this code now returns the fishing rod.

    Kept as a test rather than left implicit: it is a consequence of the
    specification, and it should fail loudly if the specification changes."""
    shortlist = catalog().shortlist(code="T11018800", description="EXTERNAL HDD 4TB")

    assert shortlist.by_code is not None
    assert shortlist.by_code.description == "ROD FISHING WITH FURTHER, DETAILS"
    assert shortlist.confirmed is True
    assert shortlist.conflicted is False


def test_a_line_with_no_code_is_ranked_on_its_words_alone():
    shortlist = catalog().shortlist(description="Convex rulers")

    assert shortlist.by_code is None
    assert shortlist.confirmed is None
    assert shortlist.best is not None and shortlist.best.code == "T65082300"


def test_nothing_asked_is_nothing_answered():
    shortlist = catalog().shortlist()

    assert shortlist.candidates == [] or list(shortlist.candidates) == []
    assert shortlist.best is None
    assert shortlist.conflicted is False


def test_the_code_s_item_appears_once_even_though_the_words_found_it_too():
    shortlist = catalog().shortlist(
        code="T85116300", description="WELDER GLOVES FIVE FINGERS", limit=5
    )

    assert shortlist.codes.count("T85116300") == 1


def test_an_empty_catalogue_answers_nothing_rather_than_anything():
    empty = Catalog([])

    assert empty.search("anything at all") == []
    assert empty.by_code("T1") is None


# --- the small parts ------------------------------------------------------


def test_a_code_is_compared_without_its_punctuation():
    assert normalize_code("t-691284/00") == normalize_code("T69128400")


def test_sizes_stay_attached_to_their_numbers():
    """"M16" is one word. Split into "m" and "16" it stops telling bolts apart."""
    assert tokenize("HEX BOLT M16 X 65MM") == ["hex", "bolt", "m16", "x", "65mm"]


def test_an_item_knows_what_it_is_without_the_catalogue():
    item = CatalogItem(code="T1", description="WIRE ROPE")

    assert item.fields == {}
