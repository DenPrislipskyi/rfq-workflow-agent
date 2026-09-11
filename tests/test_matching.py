"""Choosing a product for a line of an RFQ, and refusing to.

An item code is a key: the desk orders against it. So the case that matters
most here is not the model choosing well - it is the model answering with a
code nobody showed it, and that code never reaching a record.
"""

from src.domain.rules.catalog import Catalog
from src.infrastructure.llm.client import LLMResult
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.extraction.models import LineItem
from src.services.matching import ItemChooser, LineDescriber, MatchingPipeline, Question
from src.services.matching.decide import FAILED, NOT_ASKED, NOT_OFFERED
from src.services.matching.models import BY_SEARCH, CODE_CONFIRMED, CODE_REJECTED, NOTHING
from src.services.matching.schemas import ChosenItem, ChosenItems, RestatedLine, RestatedLines
from tests.fakes import BrokenLLM

ROWS = [
    {
        "Item Code": "T69128400",
        "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM",
        "Customer Code": "691284",
    },
    {
        "Item Code": "T69133100",
        "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M20 X 80MM",
        "Customer Code": "691331",
    },
    {
        "Item Code": "T11018800",
        "Item Description": "ROD FISHING WITH FURTHER, DETAILS",
        "Customer Code": "110188",
    },
]


def catalog() -> Catalog:
    return Catalog.from_rows(
        ROWS,
        code_column="Item Code",
        description_column="Item Description",
        customer_code_column="Customer Code",
    )


class ScriptedLLM:
    """Answers each call from a list, whatever it was asked."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def invoke(self, messages, schema):
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        return LLMResult(value=answer, model="fake", latency_ms=1)


def chosen(*triples: tuple[int, str | None, str]) -> ChosenItems:
    return ChosenItems(
        lines=[ChosenItem(index=i, item_code=code, why=why) for i, code, why in triples]
    )


def question(index: int = 0, by_code: str | None = None) -> Question:
    return Question(
        index=index,
        description="HEX HEAD BOLT WITH NUT M16 X 65",
        verbatim="Hexagon Head Bolts (Bolt with Nut) M16*65",
        candidates=[catalog().by_code("T69128400"), catalog().by_code("T69133100")],
        by_code=by_code,
    )


# --- the choice, and what is done with it --------------------------------


async def test_a_candidate_the_model_picked_is_kept():
    llm = ScriptedLLM(chosen((0, "T69128400", "Same bolt, same size.")))

    choices = await ItemChooser(llm).run([question()])

    assert choices[0].item_code == "T69128400"
    assert choices[0].why == "Same bolt, same size."


async def test_a_code_that_was_never_offered_is_refused():
    """The failure worth naming. A code from nowhere is an order from nowhere,
    and it is not quietly corrected to the nearest candidate either."""
    llm = ScriptedLLM(chosen((0, "T99999999", "This one looks right.")))

    choices = await ItemChooser(llm).run([question()])

    assert choices[0].item_code is None
    assert "T99999999" in choices[0].why


async def test_a_code_from_another_line_is_refused_too():
    llm = ScriptedLLM(chosen((0, "T11018800", "Borrowed from the line below.")))

    choices = await ItemChooser(llm).run([question()])

    assert choices[0].item_code is None
    assert choices[0].why == NOT_OFFERED.format(code="T11018800")


async def test_refusing_is_an_answer_and_keeps_its_reason():
    llm = ScriptedLLM(chosen((0, None, "Nothing here is a hard drive.")))

    choices = await ItemChooser(llm).run([question()])

    assert choices[0].item_code is None
    assert choices[0].why == "Nothing here is a hard drive."


async def test_a_line_the_model_ignored_is_a_refusal_not_the_top_candidate():
    """Silence is not agreement. Taking the first candidate for a line the
    model skipped would be guessing on its behalf."""
    llm = ScriptedLLM(chosen((0, "T69128400", "Same bolt.")))

    choices = await ItemChooser(llm).run([question(0), question(1)])

    assert choices[0].item_code == "T69128400"
    assert choices[1].item_code is None


async def test_a_line_with_no_candidates_is_never_asked_about():
    empty = Question(index=0, description="EXTERNAL HARD DISK 4TB", verbatim="hdd")
    llm = ScriptedLLM(chosen())

    choices = await ItemChooser(llm).run([empty])

    assert choices[0].item_code is None
    assert choices[0].why == NOT_ASKED
    assert llm.calls == 0


async def test_an_answer_for_a_line_outside_the_batch_is_ignored():
    llm = ScriptedLLM(chosen((7, "T69128400", "For a line nobody asked about.")))

    choices = await ItemChooser(llm).run([question(0)])

    assert set(choices) == {0}
    assert choices[0].item_code is None


async def test_a_model_that_cannot_be_asked_refuses_every_line():
    chooser = ItemChooser(BrokenLLM(LLMCallError("fake", TimeoutError("no answer"))))

    choices = await chooser.run([question(0), question(1)])

    assert [choice.item_code for choice in choices.values()] == [None, None]
    assert all(choice.why == FAILED for choice in choices.values())


async def test_lines_are_asked_in_batches():
    llm = ScriptedLLM(chosen((0, "T69128400", "a")), chosen((1, "T69133100", "b")))

    choices = await ItemChooser(llm, batch=1).run([question(0), question(1)])

    assert llm.calls == 2
    assert choices[0].item_code == "T69128400"


# --- the whole pipeline ---------------------------------------------------


def pipeline(describe, decide, candidates: int = 5) -> MatchingPipeline:
    return MatchingPipeline(
        LineDescriber(describe), ItemChooser(decide), candidates=candidates
    )


def items() -> list[LineItem]:
    return [
        LineItem(
            sr_no=1,
            description="Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65",
            customer_item_code="691284",
        ),
        LineItem(sr_no=2, description="EXTERNAL HDD 4TB", customer_item_code="110188"),
    ]


def restated(*pairs: tuple[int, str]) -> RestatedLines:
    return RestatedLines(lines=[RestatedLine(index=i, description=t) for i, t in pairs])


async def test_a_code_the_words_agree_with_is_confirmed():
    describe = ScriptedLLM(restated((0, "HEX HEAD BOLT WITH NUT M16 X 65MM")))
    decide = ScriptedLLM(chosen((0, "T69128400", "Same bolt, same size.")))

    matched = await pipeline(describe, decide).run(items()[:1], catalog())

    assert matched[0].item_code == "T69128400"
    assert matched[0].how == CODE_CONFIRMED
    assert matched[0].customer_code == "691284"


async def test_a_code_the_words_contradict_is_overruled():
    """The desk's own example: 110188 is quoted for a hard drive, and the
    product that code names is a fishing rod."""
    describe = ScriptedLLM(restated((0, "EXTERNAL HARD DISK DRIVE 4TB")))
    decide = ScriptedLLM(chosen((0, None, "The code names a fishing rod.")))

    matched = await pipeline(describe, decide).run(items()[1:], catalog())

    assert matched[0].item_code is None
    assert matched[0].how == NOTHING
    assert matched[0].why == "The code names a fishing rod."


async def test_a_line_with_no_code_is_matched_on_its_words():
    line = [LineItem(sr_no=1, description="bolt with nut M20 x 80")]
    describe = ScriptedLLM(restated((0, "HEX HEAD BOLT WITH NUT M20 X 80MM")))
    decide = ScriptedLLM(chosen((0, "T69133100", "Same bolt, same size.")))

    matched = await pipeline(describe, decide).run(line, catalog())

    assert matched[0].item_code == "T69133100"
    assert matched[0].how == BY_SEARCH


async def test_a_different_product_chosen_over_the_code_is_recorded_as_such():
    describe = ScriptedLLM(restated((0, "HEX HEAD BOLT WITH NUT M20 X 80MM")))
    decide = ScriptedLLM(chosen((0, "T69133100", "The customer's code is for the M16.")))

    matched = await pipeline(describe, decide).run(items()[:1], catalog())

    assert matched[0].item_code == "T69133100"
    assert matched[0].how == CODE_REJECTED


async def test_every_line_comes_back_even_when_nothing_matches():
    describe = ScriptedLLM(restated())
    decide = ScriptedLLM(chosen())

    matched = await pipeline(describe, decide).run(items(), catalog())

    assert [line.index for line in matched] == [1, 2]
    assert all(not line.matched for line in matched)


async def test_the_verbatim_line_survives_the_restatement():
    """Rule 7: what the reader got out of the file stays beside what we made
    of it, or a wrong match cannot be explained afterwards."""
    describe = ScriptedLLM(restated((0, "HEX HEAD BOLT WITH NUT M16 X 65MM")))
    decide = ScriptedLLM(chosen((0, "T69128400", "Same bolt.")))

    matched = await pipeline(describe, decide).run(items()[:1], catalog())

    assert matched[0].verbatim == items()[0].description
    assert matched[0].description == "HEX HEAD BOLT WITH NUT M16 X 65MM"
    assert matched[0].candidates


async def test_an_empty_catalogue_matches_nothing_and_says_so():
    describe = ScriptedLLM(restated())
    decide = ScriptedLLM(chosen())

    matched = await pipeline(describe, decide).run(items(), Catalog([]))

    assert all(line.item_code is None and line.why for line in matched)
    assert decide.calls == 0


async def test_an_rfq_with_no_lines_costs_no_calls():
    describe = ScriptedLLM(restated())
    decide = ScriptedLLM(chosen())

    assert await pipeline(describe, decide).run([], catalog()) == []
    assert describe.calls == 0 and decide.calls == 0
