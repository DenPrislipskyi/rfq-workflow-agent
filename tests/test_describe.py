"""Restating a customer's line in our own words.

What matters here is not the happy path. This step sits in front of the
catalogue search, so every way it can fail has to leave the search no worse off
than if the step had never run: whatever the model does not answer, answers
badly, or cannot answer at all keeps the customer's own words.
"""

from src.infrastructure.llm.client import LLMResult
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.matching import LineDescriber
from src.services.matching.schemas import RestatedLine, RestatedLines
from tests.fakes import BrokenLLM

LINES = [
    "Weldings gloves(five fingers)",
    "Convex rulers",
    "Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65",
]


class ScriptedLLM:
    """Answers each call from a list, and remembers what it was asked."""

    def __init__(self, *answers: RestatedLines) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    async def invoke(self, messages, schema):
        self.calls.append(str(messages[-1][1]))
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return LLMResult(value=answer, model="fake", latency_ms=1)


def restated(*pairs: tuple[int, str]) -> RestatedLines:
    return RestatedLines(lines=[RestatedLine(index=i, description=text) for i, text in pairs])


async def test_every_line_comes_back_restated_and_in_order():
    llm = ScriptedLLM(restated((0, "WELDER GLOVES FIVE FINGERS"), (1, "RULE CONVEX"), (2, "HEX BOLT")))

    assert await LineDescriber(llm).run(LINES) == [
        "WELDER GLOVES FIVE FINGERS",
        "RULE CONVEX",
        "HEX BOLT",
    ]


async def test_a_line_the_model_skipped_keeps_the_customer_s_words():
    """A dropped line must not shift the ones after it onto other products."""
    llm = ScriptedLLM(restated((0, "WELDER GLOVES FIVE FINGERS"), (2, "HEX BOLT")))

    answer = await LineDescriber(llm).run(LINES)

    assert answer == ["WELDER GLOVES FIVE FINGERS", "Convex rulers", "HEX BOLT"]


async def test_an_index_the_model_invented_is_ignored():
    llm = ScriptedLLM(restated((0, "WELDER GLOVES"), (9, "SOMETHING ELSE"), (-1, "AND THIS")))

    answer = await LineDescriber(llm).run(LINES)

    assert answer == ["WELDER GLOVES", "Convex rulers", LINES[2]]


async def test_a_model_that_cannot_answer_costs_nothing():
    """The search then runs on the verbatim lines, exactly as it would have
    done had this step not existed."""
    answer = await LineDescriber(BrokenLLM(LLMCallError("fake", TimeoutError("no answer")))).run(LINES)

    assert answer == LINES


async def test_an_empty_restatement_is_refused():
    llm = ScriptedLLM(restated((0, "   "), (1, "RULE CONVEX"), (2, "HEX BOLT")))

    answer = await LineDescriber(llm).run(LINES)

    assert answer[0] == LINES[0]


async def test_a_restatement_that_turned_into_a_paragraph_is_refused():
    """Length like that means the model started explaining. The customer's own
    words have never been wrong about what the customer asked for."""
    llm = ScriptedLLM(restated((0, "WELDER GLOVES " + "AND MORE WORDS " * 20)))

    answer = await LineDescriber(llm).run(LINES[:1])

    assert answer == LINES[:1]


async def test_long_lists_are_asked_in_batches():
    """A model answering about two hundred lines at once starts dropping them."""
    llm = ScriptedLLM(restated((0, "ONE"), (1, "TWO")))

    answer = await LineDescriber(llm, batch=2).run(LINES)

    assert len(llm.calls) == 2
    assert answer == ["ONE", "TWO", "ONE"]


async def test_nothing_asked_is_nothing_answered():
    llm = ScriptedLLM(restated())

    assert await LineDescriber(llm).run([]) == []
    assert llm.calls == []
