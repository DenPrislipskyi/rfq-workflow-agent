"""Matching a line of an RFQ to a product, and refusing to.

Three branches and nothing else, exactly as the desk specified them:

    the code names an item whose customer wording agrees -> that item, alone
    the code names an item whose wording disagrees       -> dropped, searched
    no code, or a code the sheet does not carry          -> searched

The case that matters most is the middle one. An item code is a key the desk
orders against, and the desk's own notes are that a customer's code cannot be
trusted on its own - so what settles it is the wording, every time.
"""

from src.domain.rules.catalog import Catalog
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.extraction.models import LineItem
from src.services.matching import LineDescriber, MatchingPipeline
from src.services.matching.models import BY_SEARCH, CODE_CONFIRMED, CODE_REJECTED, NOTHING
from tests.fakes import BrokenLLM, FakeLLM
from src.services.matching.schemas import RestatedLine, RestatedLines

# Two bolts that differ by one size token, and a fishing rod the desk put in
# the sheet as an example of a customer code that leads somewhere else.
ROWS = [
    {
        "Item Code": "T69128400",
        "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM",
        "Customer Code": "691284",
        "Customer Description": "Hexagon Head Bolts Full Threaded M16 X 65MM",
    },
    {
        "Item Code": "T69133100",
        "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M20 X 80MM",
        "Customer Code": "691331",
        "Customer Description": "Hexagon Head Bolts Full Threaded M20 X 80MM",
    },
    {
        "Item Code": "T11018800",
        "Item Description": "ROD FISHING WITH FURTHER, DETAILS",
        "Customer Code": "110188",
        "Customer Description": "EXTERNAL HDD 4TB",
    },
]


def catalog(rows=None) -> Catalog:
    return Catalog.from_rows(
        ROWS if rows is None else rows,
        code_column="Item Code",
        description_column="Item Description",
        customer_code_column="Customer Code",
        customer_description_column="Customer Description",
    )


class Restating(FakeLLM):
    """A describer that says each line back exactly as told."""

    def __init__(self, *descriptions: str) -> None:
        super().__init__(
            RestatedLines(
                lines=[
                    RestatedLine(index=index, description=text)
                    for index, text in enumerate(descriptions)
                ]
            )
        )


def pipeline(llm, candidates: int = 5, agreement: float = 80.0) -> MatchingPipeline:
    return MatchingPipeline(
        LineDescriber(llm), candidates=candidates, agreement=agreement
    )


def line(sr_no: int = 1, description: str = "", code: str | None = None) -> LineItem:
    return LineItem(
        sr_no=sr_no, description=description, customer_item_code=code, quantity="1", uom="pcs"
    )


async def test_a_code_whose_wording_agrees_is_confirmed_alone():
    """The first branch. One product, and no shortlist beside it: there was
    nothing to choose between, so offering alternatives would invent a doubt."""
    items = [line(description="Hexagon Head Bolts Full Threaded M16*65", code="691284")]
    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED M16 X 65MM")).run(
        items, catalog()
    )

    assert matched[0].item_code == "T69128400"
    assert matched[0].how == CODE_CONFIRMED
    assert matched[0].confidence == 100
    assert matched[0].candidates == []


async def test_a_code_whose_wording_disagrees_is_dropped_and_the_sheet_searched():
    """The desk's own case: code 691284 is the M16 bolt and the customer
    described an M20. The code loses, and the line goes to the search."""
    items = [line(description="Hexagon head bolts with nut M20 x 80", code="691284")]
    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED M20 X 80MM")).run(
        items, catalog()
    )

    assert matched[0].how == CODE_REJECTED
    assert matched[0].item_code is None, "a dropped code is not an answer"
    assert matched[0].item is None
    assert "T69128400" in matched[0].why, "the record says which code was dropped"
    assert [one.item.code for one in matched[0].candidates][0] == "T69133100"


async def test_a_line_with_no_code_is_searched_on_its_words():
    items = [line(description="hex head bolts with nuts M20 x 80")]
    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED M20 X 80MM")).run(
        items, catalog()
    )

    assert matched[0].how == BY_SEARCH
    assert matched[0].item_code is None
    assert [one.item.code for one in matched[0].candidates][0] == "T69133100"


async def test_a_code_the_sheet_does_not_carry_is_searched_too():
    """Same branch as no code at all: nothing in the sheet answers to it."""
    items = [line(description="hex head bolts M20 x 80", code="0012345")]
    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED M20 X 80MM")).run(
        items, catalog()
    )

    assert matched[0].how == BY_SEARCH
    assert matched[0].customer_code == "0012345", "kept as the customer wrote it"


async def test_the_shortlist_is_capped_at_what_was_asked_for():
    items = [line(description="hexagon head bolts full threaded")]
    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED"), candidates=1).run(
        items, catalog()
    )

    assert len(matched[0].candidates) == 1


async def test_candidates_are_scored_against_the_best_of_their_own_line():
    """A relative ranking, and only within one line. The first is always 100
    and nothing compares it to another line's."""
    items = [line(description="hexagon head bolts full threaded M16 X 65MM")]
    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED M16 X 65MM")).run(
        items, catalog()
    )

    scores = [one.confidence for one in matched[0].candidates]
    assert scores[0] == 100
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= score <= 100 for score in scores)


async def test_a_line_the_search_cannot_answer_is_a_refusal_with_a_reason():
    items = [line(description="turbocharger cartridge NR34/S")]
    matched = await pipeline(Restating("TURBOCHARGER CARTRIDGE NR34/S")).run(items, catalog())

    assert matched[0].how == NOTHING
    assert matched[0].item_code is None
    assert matched[0].candidates == []
    assert matched[0].why


async def test_the_threshold_is_what_decides_between_the_first_two_branches():
    """Same line, same sheet, two thresholds. At 80 the wording is not enough;
    at 40 it is. Nothing else in the branch changes."""
    items = [line(description="chrome plated fastener as per drawing", code="691284")]
    restated = "HEXAGON HEAD BOLTS M16 CHROME PLATED"

    strict = await pipeline(Restating(restated), agreement=80.0).run(items, catalog())
    loose = await pipeline(Restating(restated), agreement=40.0).run(items, catalog())

    assert strict[0].how == CODE_REJECTED
    assert loose[0].how == CODE_CONFIRMED


async def test_a_line_that_says_what_the_sheet_says_is_not_argued_with():
    """The bug this pair of comparisons exists for.

    The customer wrote the sentence the sheet already files this product under.
    Our restatement of it says the same thing in our own words and shares half
    of them - and for a while that half was the whole answer, so a line that
    matched word for word had its code dropped.
    """
    items = [line(description="Hexagon Head Bolts Full Threaded M16 X 65MM", code="691284")]

    matched = await pipeline(Restating("HEX HEAD BOLT/NUT FULL THREADED M16 X 65MM")).run(
        items, catalog()
    )

    assert matched[0].how == CODE_CONFIRMED
    assert matched[0].item_code == "T69128400"


async def test_a_size_welded_to_its_unit_is_the_same_size():
    """`65MM` and `65` are one number written twice. Only the comparison is
    widened like this - the index is built and searched exactly as before."""
    items = [line(description="Hexagon Head Bolts Full Threaded M16 X 65", code="691284")]

    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED M16 X 65MM")).run(
        items, catalog()
    )

    assert matched[0].how == CODE_CONFIRMED


async def test_every_line_comes_back_in_the_order_it_arrived():
    items = [
        line(1, "hexagon head bolts M16 X 65MM", "691284"),
        line(2, "turbocharger cartridge"),
        line(3, "hexagon head bolts M20 X 80MM", "691331"),
    ]
    matched = await pipeline(
        Restating(
            "HEXAGON HEAD BOLTS FULL THREADED M16 X 65MM",
            "TURBOCHARGER CARTRIDGE",
            "HEXAGON HEAD BOLTS FULL THREADED M20 X 80MM",
        )
    ).run(items, catalog())

    assert [one.index for one in matched] == [1, 2, 3]
    assert [one.how for one in matched] == [CODE_CONFIRMED, NOTHING, CODE_CONFIRMED]


async def test_the_verbatim_line_survives_the_restatement():
    """Rule 7 of the project: what the customer wrote is not overwritten by
    what we made of it. Both are on the record."""
    items = [line(description="Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65")]
    matched = await pipeline(Restating("HEXAGON HEAD BOLTS FULL THREADED M16 X 65MM")).run(
        items, catalog()
    )

    assert matched[0].verbatim == "Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65"
    assert matched[0].description == "HEXAGON HEAD BOLTS FULL THREADED M16 X 65MM"


async def test_a_describer_that_fails_falls_back_to_the_customers_own_words():
    """The one model call left, and losing it must not lose the line."""
    items = [line(description="Hexagon Head Bolts Full Threaded M16 X 65MM", code="691284")]
    broken = BrokenLLM(LLMCallError("fake", TimeoutError("no answer")))

    matched = await pipeline(broken).run(items, catalog())

    assert matched[0].description == "Hexagon Head Bolts Full Threaded M16 X 65MM"
    assert matched[0].how == CODE_CONFIRMED, "the customer's own words still agree"


async def test_an_empty_catalogue_matches_nothing_and_says_so():
    items = [line(description="hexagon head bolts", code="691284")]

    matched = await pipeline(Restating("HEXAGON HEAD BOLTS")).run(items, catalog(rows=[]))

    assert matched[0].how == NOTHING
    assert matched[0].why
    assert len(matched) == 1, "the line still comes back"


async def test_an_rfq_with_no_lines_costs_no_calls():
    llm = Restating()

    matched = await pipeline(llm).run([], catalog())

    assert matched == []
    assert not llm.calls
