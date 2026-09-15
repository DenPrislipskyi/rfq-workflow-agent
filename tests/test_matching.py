"""Matching a line of an RFQ to a product, and refusing to.

Three branches and nothing else, exactly as the desk specified them:

    the code names a row whose own two descriptions agree  -> that row, alone
    the code names a row whose own two descriptions differ -> dropped, searched
    no code, or a code the sheet does not carry            -> searched

What is checked in the first two branches is the *sheet*, not the email. A row
is one past mapping and some of them are wrong, so a customer code that lands
on a bad row must not carry its item code into an order.
"""

import re

from src.domain.rules.catalog import Catalog
from src.infrastructure.llm.client import LLMResult, Messages
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.extraction.models import LineItem
from src.services.matching import AgreementJudge, MatchingPipeline
from src.services.matching.models import BY_SEARCH, CODE_CONFIRMED, CODE_REJECTED, NOTHING
from src.services.matching.schemas import Judgement, Judgements
from tests.fakes import BrokenLLM

# Two bolts that differ by one size token; the fishing rod the desk put in the
# sheet as an example of a row that maps the wrong thing; and the drive that
# row's customer actually asked for.
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
    {
        "Item Code": "T55000100",
        "Item Description": "EXTERNAL HARD DISK DRIVE 4TB USB",
        "Customer Code": "550001",
        "Customer Description": "portable drive",
    },
]

PAIR = re.compile(r"^  (\d+)\. customer: (.*)\n     item:     (.*)$", re.MULTILINE)


def catalog(rows=None) -> Catalog:
    return Catalog.from_rows(
        ROWS if rows is None else rows,
        code_column="Item Code",
        description_column="Item Description",
        customer_code_column="Customer Code",
        customer_description_column="Customer Description",
    )


class Judging:
    """A judge that answers from a rule, and keeps what it was asked.

    The pairs are read back out of the prompt rather than handed in, so that
    the numbering the pipeline relies on is exercised rather than assumed.
    """

    def __init__(self, same=lambda customer, item: True, *, drop: set[int] = frozenset()) -> None:
        self._same = same
        self._drop = drop
        self.batches: list[list[tuple[str, str]]] = []

    @property
    def asked(self) -> list[tuple[str, str]]:
        return [pair for batch in self.batches for pair in batch]

    async def invoke[T](self, messages: Messages, schema: type[T]) -> LLMResult[T]:
        pairs = [(customer, item) for _, customer, item in PAIR.findall(messages[-1][1])]
        self.batches.append(pairs)
        return LLMResult(
            value=Judgements(
                items=[
                    Judgement(index=index, same=self._same(customer, item), why="said so")
                    for index, (customer, item) in enumerate(pairs)
                    if index not in self._drop
                ]
            ),
            model="fake",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )


def pipeline(llm, *, candidates: int = 5, batch: int = 50) -> MatchingPipeline:
    return MatchingPipeline(
        AgreementJudge(llm, batch=batch, concurrency=4), candidates=candidates
    )


def line(sr_no: int = 1, description: str = "", code: str | None = None) -> LineItem:
    return LineItem(
        sr_no=sr_no, description=description, customer_item_code=code, quantity="1", uom="pcs"
    )


# --- branch one: the row agrees with itself -------------------------------


async def test_a_row_that_agrees_with_itself_confirms_its_code():
    judge = Judging()
    items = [line(1, "Hexagon Head Bolts (Bolt with Nut) M16*65", "691284")]

    matched = await pipeline(judge).run(items, catalog())

    assert matched[0].how == CODE_CONFIRMED
    assert matched[0].item_code == "T69128400"
    assert matched[0].item is not None
    assert matched[0].item.fields["Item Description"] == (
        "HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM"
    )
    # No score, and that is the answer rather than a missing one: what confirms
    # the code here is the code, not the words.
    assert matched[0].confidence is None


async def test_a_confirmed_code_is_offered_no_alternatives():
    """There was nothing to choose between: one row, whole, and no shortlist."""
    matched = await pipeline(Judging()).run(
        [line(1, "Hexagon Head Bolts M16*65", "691284")], catalog()
    )

    assert matched[0].candidates == []
    assert matched[0].query == ""


async def test_the_row_is_judged_on_its_own_two_columns_not_on_the_email():
    """The question is whether the sheet row maps what it says it maps."""
    judge = Judging()

    await pipeline(judge).run([line(1, "anything at all", "110188")], catalog())

    assert judge.asked == [("EXTERNAL HDD 4TB", "ROD FISHING WITH FURTHER, DETAILS")]


# --- branch two: the row contradicts itself -------------------------------


async def test_a_row_that_contradicts_itself_loses_its_code():
    judge = Judging(same=lambda customer, item: "HDD" not in customer)

    matched = await pipeline(judge).run([line(1, "external hdd", "110188")], catalog())

    assert matched[0].how == CODE_REJECTED
    assert matched[0].item_code is None
    assert matched[0].item is None
    assert "T11018800" in matched[0].why
    assert "said so" in matched[0].why


async def test_an_overruled_row_is_searched_by_its_own_customer_wording():
    """Not by the line of the email: the desk's specification is that the
    sheet's own wording for that row is what goes back into the search."""
    judge = Judging(same=lambda customer, item: "HDD" not in customer)

    matched = await pipeline(judge).run([line(1, "please quote asap", "110188")], catalog())

    assert matched[0].query == "EXTERNAL HDD 4TB"
    assert [one.item.code for one in matched[0].candidates] == ["T55000100"]


# --- branch three: no code, or a code the sheet does not carry ------------


async def test_a_line_with_no_code_is_searched_on_its_own_description():
    judge = Judging()
    matched = await pipeline(judge).run([line(1, "hex head bolt m20 80mm")], catalog())

    assert matched[0].how == BY_SEARCH
    assert matched[0].query == "hex head bolt m20 80mm"
    assert matched[0].candidates[0].item.code == "T69133100"
    assert judge.asked == [], "nothing was found by code, so nothing had to be judged"


async def test_a_code_the_sheet_does_not_carry_is_searched_too():
    matched = await pipeline(Judging()).run(
        [line(1, "hex head bolt m16 65mm", "NO-SUCH-CODE")], catalog()
    )

    assert matched[0].how == BY_SEARCH
    assert matched[0].item_code is None
    assert "No row carries this customer code" in matched[0].why


async def test_the_two_searched_branches_say_which_one_they_were():
    quoted = await pipeline(Judging()).run([line(1, "bolt", "NOPE")], catalog())
    silent = await pipeline(Judging()).run([line(1, "bolt")], catalog())

    assert quoted[0].why != silent[0].why
    assert "quoted no code" in silent[0].why


# --- the shortlist --------------------------------------------------------


async def test_the_shortlist_is_capped_at_what_was_asked_for():
    matched = await pipeline(Judging(), candidates=2).run(
        [line(1, "hex head bolt steel")], catalog()
    )

    assert len(matched[0].candidates) == 2


async def test_one_candidate_per_item_code_however_many_rows_it_has():
    rows = [
        {"Item Code": "T31237400", "Item Description": "BOILERSUIT NAVY 3XL",
         "Customer Description": "boilersuit 3XL"},
        {"Item Code": "T31237400", "Item Description": "BOILERSUIT NAVY 2XL",
         "Customer Description": "boilersuit 2XL"},
    ]

    matched = await pipeline(Judging()).run([line(1, "boilersuit navy")], catalog(rows))

    assert [one.item.code for one in matched[0].candidates] == ["T31237400"]


async def test_a_candidate_is_scored_on_how_much_of_the_question_it_answers():
    """Absolute, so it reads the same on every line. The old score was a
    percentage of the best candidate, which made the best one 100% by dividing
    it by itself - every shortlist claimed certainty it never had."""
    matched = await pipeline(Judging()).run(
        [line(1, "hex head bolt nut steel ungalv m16 65mm")], catalog()
    )

    best, *rest = matched[0].candidates
    assert best.item.code == "T69128400"
    assert best.confidence == 100, "every word asked for is in this product"
    assert all(one.confidence < 100 for one in rest), "and not in the others"


async def test_the_top_candidate_is_not_100_percent_just_for_being_top():
    """The bug this replaced: a box of eggs at the top of a shortlist scored
    the same as a perfect match, because both were divided by themselves.

    A customer who names a family without its size is asking a question the
    sheet cannot answer on its own, and the shortlist has to say so rather
    than put a 100% on whichever row sorted first."""
    matched = await pipeline(Judging()).run(
        [line(1, "hexagon head bolts with nut, full thread")], catalog()
    )

    scored = {one.item.code: one.confidence for one in matched[0].candidates}
    bolts = [scored[code] for code in ("T69128400", "T69133100") if code in scored]

    assert len(bolts) == 2
    assert all(score < 100 for score in bolts), "no size was asked for, so nothing is certain"
    assert len(set(bolts)) == 1, "and nothing distinguishes the two of them"
    assert scored["T11018800"] < min(bolts), "the fishing rod is visibly further away"


async def test_a_shortlist_for_something_we_do_not_sell_scores_low():
    """The number's whole job: a line whose product is not in the sheet still
    gets a shortlist, and it has to look like one."""
    matched = await pipeline(Judging()).run(
        [line(1, "turbocharger cartridge NR34 for marine diesel bolt")], catalog()
    )

    assert matched[0].candidates, "BM25 found something on the word it shares"
    assert max(one.confidence for one in matched[0].candidates) <= 30


async def test_a_line_the_search_cannot_answer_is_a_refusal_with_a_reason():
    matched = await pipeline(Judging()).run([line(1, "zzzz qqqq")], catalog())

    assert matched[0].how == NOTHING
    assert matched[0].item_code is None
    assert "nothing to offer" in matched[0].why


# --- what the judge costs, and what happens when it fails -----------------


async def test_every_distinct_row_is_judged_once_however_many_lines_want_it():
    judge = Judging()
    items = [line(1, "bolts", "691284"), line(2, "more bolts", "691284")]

    matched = await pipeline(judge).run(items, catalog())

    assert len(judge.asked) == 1
    assert [one.how for one in matched] == [CODE_CONFIRMED, CODE_CONFIRMED]


async def test_the_rows_are_judged_in_batches():
    rows = [
        {"Item Code": f"T{index:08d}", "Item Description": f"WIDGET NUMBER {index}",
         "Customer Code": str(index), "Customer Description": f"widget {index}"}
        for index in range(120)
    ]
    items = [line(index + 1, f"widget {index}", str(index)) for index in range(120)]

    judge = Judging()
    await pipeline(judge, batch=50).run(items, catalog(rows))

    assert [len(batch) for batch in judge.batches] == [50, 50, 20]
    assert len(judge.asked) == 120


async def test_a_judge_that_fails_confirms_nothing():
    """The safe direction: an unchecked code goes to the shortlist rather than
    into an order. Silence is not consent."""
    items = [line(1, "bolts", "691284"), line(2, "more bolts", "691331")]

    matched = await pipeline(BrokenLLM(LLMCallError("fake", RuntimeError("down")))).run(items, catalog())

    assert [one.how for one in matched] == [CODE_REJECTED, CODE_REJECTED]
    assert all(one.item_code is None for one in matched)


async def test_a_judgement_the_model_skipped_does_not_shift_its_neighbours():
    judge = Judging(drop={0})
    items = [line(1, "bolts", "691284"), line(2, "more bolts", "691331")]

    matched = await pipeline(judge).run(items, catalog())

    assert matched[0].how == CODE_REJECTED, "unanswered, so not confirmed"
    assert matched[1].how == CODE_CONFIRMED, "answered, and still its own answer"
    assert matched[1].item_code == "T69133100"


# --- what must hold for every run -----------------------------------------


async def test_every_line_comes_back_in_the_order_it_arrived():
    items = [
        line(1, "hex head bolt m16", "691284"),
        line(2, "nothing like anything"),
        line(3, "external hdd", "110188"),
    ]

    matched = await pipeline(Judging()).run(items, catalog())

    assert [one.index for one in matched] == [1, 2, 3]


async def test_the_customers_own_words_survive_every_branch():
    items = [
        line(1, "Hexagon Head Bolts M16*65", "691284"),
        line(2, "external hdd", "110188"),
        line(3, "hex head bolt m20"),
    ]

    matched = await pipeline(
        Judging(same=lambda customer, item: "HDD" not in customer)
    ).run(items, catalog())

    assert [one.verbatim for one in matched] == [
        "Hexagon Head Bolts M16*65",
        "external hdd",
        "hex head bolt m20",
    ]


async def test_an_empty_catalogue_matches_nothing_and_says_so():
    judge = Judging()
    items = [line(1, "bolts", "691284"), line(2, "gloves")]

    matched = await pipeline(judge).run(items, Catalog([]))

    assert [one.how for one in matched] == [NOTHING, NOTHING]
    assert all("no catalogue" in one.why for one in matched)
    assert judge.batches == []


async def test_an_rfq_with_no_lines_costs_no_calls():
    judge = Judging()

    assert await pipeline(judge).run([], catalog()) == []
    assert judge.batches == []
