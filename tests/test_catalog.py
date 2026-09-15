"""Matching a customer's wording against our own product list.

The cases here are the ones the desk's own mapping notes single out, in the
desk's own words: a code that contradicts the description, a line with no code
at all, and two products that differ by one number in the middle of an
otherwise identical sentence.
"""

from src.domain.rules.catalog import Catalog, CatalogItem, normalize_code, tokenize

# Two columns per row, and the difference between them is the point. Ours is
# what the search reads; the customer's is one half of the question of whether
# the row maps what it says it maps, and the query when the answer is no.
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
                "Item Description": "(주)씨웨이글로벌 ROPE",
                "Customer Description": "rope",
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
        index_customer_description=indexed,
    )


def test_our_own_wording_is_what_the_search_reads():
    """The sheet mentions a product's customer wording once. Index that instead
    and a row can be found by exactly the one sentence already in it and by
    nothing else - measured at 2.4% against 97.6% for this column."""
    found = _with_wording(indexed=False).search("hand wash dettol pump bottle")

    assert found[0].item.code == "T55029103"
    assert _with_wording(indexed=False).items[0].customer_description == "sanitiser gel"


def test_the_customer_s_wording_is_searched_only_when_that_is_switched_on():
    """Off by default. It is kept on the item either way, because the matching
    pipeline reads it: it is what a row is judged on, and what the sheet is
    searched with once a quoted code has been overruled."""
    assert _with_wording(indexed=False).search("sanitiser gel") == []
    assert _with_wording(indexed=True).search("sanitiser gel")[0].item.code == "T55029103"


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


def test_a_code_whose_wording_says_something_else_is_flagged():
    """A code is confirmed by the description the sheet files it under, so what
    contradicts it is a line that description does not answer."""
    shortlist = catalog().shortlist(code="T69128400", description="Convex rulers metric")

    assert shortlist.by_code is not None
    assert shortlist.confirmed is False
    assert shortlist.conflicted is True


def test_the_desks_negative_mapping_shows_up_as_a_conflict():
    """Row 110188 is the desk's own example of a mapping that went wrong: the
    customer asked for a hard drive and the item the code names is a fishing
    rod. Searching our own description column sees it - the words of the
    request find nothing like the item the code leads to."""
    shortlist = catalog().shortlist(code="T11018800", description="EXTERNAL HDD 4TB")

    assert shortlist.by_code is not None
    assert shortlist.by_code.description == "ROD FISHING WITH FURTHER, DETAILS"
    assert shortlist.confirmed is False
    assert shortlist.conflicted is True


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


# --- a word the index has never seen --------------------------------------


def _misspelt() -> Catalog:
    return catalog(
        [
            {"Item Code": "T1", "Item Description": "SUGAR, WHITE, GRANULATED 2 KGS"},
            {"Item Code": "T2", "Item Description": "CHEESE, SLICED 200 GRM KRAFT"},
            {"Item Code": "T3", "Item Description": "HEX HEAD BOLT/NUT STEEL M16 X 65MM"},
            {"Item Code": "T4", "Item Description": "HEX HEAD BOLT/NUT STEEL M18 X 65MM"},
        ]
    )


def test_a_misspelt_word_still_finds_the_product():
    """`SUGER` shares no token at all with `SUGAR`, so without this the query
    scores zero against everything and the product is not found - not ranked
    lower, absent."""
    assert _misspelt().search("SUGER")[0].item.code == "T1"
    assert _misspelt().search("CHESSE")[0].item.code == "T2"


def test_a_size_is_never_repaired_into_another_size():
    """`M16` and `M18` are also one edit apart, and they are different bolts.
    Anything with a digit in it is what a line is identified by, and nothing
    here may touch it."""
    assert _misspelt().search("M17") == []


def test_a_word_two_products_could_have_meant_is_not_guessed_at():
    """One reading is a repair; two is a guess, and guessing between them is
    how the wrong product gets ordered quietly."""
    both = catalog(
        [
            {"Item Code": "T1", "Item Description": "PAINT BRUSH"},
            {"Item Code": "T2", "Item Description": "PAINT ROLLER"},
            {"Item Code": "T3", "Item Description": "PRINT CARTRIDGE"},
        ]
    )

    assert both.search("POINT") == []


def test_a_word_spelled_right_outranks_one_we_decided_was_meant():
    ranked = _misspelt().search("SUGER CHEESE")

    assert [candidate.item.code for candidate in ranked][0] == "T2"


def test_a_short_word_is_left_alone():
    """One edit is most of a short word, and the sheet is full of them."""
    assert _misspelt().search("KEGS") == []
