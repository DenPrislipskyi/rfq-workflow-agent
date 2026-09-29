"""The assessor: what a model observed about each candidate, made safe to score.

The model is faked. What is under test is everything around it - the batching,
the numbering, and above all what happens to an answer that is incomplete,
invented or missing: none of those may turn into a confident score.
"""

import asyncio
import re

from src.infrastructure.llm.client import LLMResult, Messages
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.matching.assess_prompt import build_messages, differences
from src.services.matching.assessor import BATCH, CandidateAssessor
from src.services.matching.confidence import Agreement
from src.services.matching.schemas import CandidateCheck, LineCheck, LineChecks, PropertyCheck
from tests.fakes import BrokenLLM

LINE = re.compile(r"^  (\d+)\. line: (.*)$", re.MULTILINE)
CANDIDATE = re.compile(r"^    (\d+)\. (.*)$", re.MULTILINE)

SNEAKERS = ("Steel toe sneakers", ["SNEAKERS STEEL TOE 25CM", "SNEAKERS STEEL TOE 29 CM"])


class Observing:
    """A model that answers from a rule and records what it was asked.

    Lines and candidates are read back out of the prompt, so the numbering the
    assessor relies on is exercised rather than assumed.
    """

    def __init__(self, observe=None, *, answer=None) -> None:
        self._observe = observe or (
            lambda line, candidate: CandidateCheck(candidate=0, same_product=True)
        )
        self._answer = answer
        self.calls = 0

    async def invoke[T](self, messages: Messages, schema: type[T]) -> LLMResult[T]:
        self.calls += 1
        if self._answer is not None:
            value = self._answer
        else:
            body = messages[-1][1].split("Lines:\n\n", 1)[1]
            items = []
            for block in body.split("\n\n"):
                head = LINE.search(block)
                if head is None:
                    continue
                candidates = [
                    self._observe(head.group(2), text).model_copy(update={"candidate": int(number)})
                    for number, text in CANDIDATE.findall(block)
                ]
                items.append(LineCheck(index=int(head.group(1)), candidates=candidates))
            value = LineChecks(items=items)
        return LLMResult(value=value, model="fake", latency_ms=1, input_tokens=1, output_tokens=1)


def sized(line: str, candidate: str) -> CandidateCheck:
    """The model's reading of the sneakers: same product, a size the line leaves open."""
    value = candidate.split("TOE ")[-1]
    return CandidateCheck(
        candidate=0,
        same_product=True,
        properties=[PropertyCheck(name="size", key=True, item_value=value)],
        why="size not stated",
    )


async def test_every_candidate_of_a_line_comes_back_in_shortlist_order():
    [assessed] = await CandidateAssessor(Observing(sized)).run([SNEAKERS])

    assert [one.attributes[0].offered for one in assessed] == ["25CM", "29 CM"]
    assert all(one.why == "size not stated" for one in assessed)


async def test_the_agreement_follows_from_which_side_states_a_value():
    """Derived in code, not taken from the model: it only says what each side wrote."""
    answer = LineChecks(
        items=[
            LineCheck(
                index=0,
                candidates=[
                    CandidateCheck(
                        candidate=1,
                        same_product=True,
                        properties=[
                            PropertyCheck(
                                name="size", line_value="M14", item_value="M14", agrees=True
                            ),
                            PropertyCheck(name="colour", line_value="white", item_value="blue"),
                            PropertyCheck(name="length", item_value="50MM"),
                            PropertyCheck(name="brand", key=False, line_value="Acdelco"),
                            PropertyCheck(name="finish"),
                        ],
                    )
                ],
            )
        ]
    )

    [[one]] = await CandidateAssessor(Observing(answer=answer)).run([("bolt", ["BOLT"])])

    assert [(a.name, a.agreement) for a in one.attributes] == [
        ("size", Agreement.MATCH),
        ("colour", Agreement.CONFLICT),
        ("length", Agreement.UNSTATED),
        ("brand", Agreement.UNCONFIRMED),
    ], "a property neither side states tells nothing and is left out"


async def test_a_line_missing_any_candidate_is_not_assessed():
    """Half a shortlist cannot be scored: "one of four" depends on the other three."""
    answer = LineChecks(
        items=[LineCheck(index=0, candidates=[CandidateCheck(candidate=1, same_product=True)])]
    )

    assert await CandidateAssessor(Observing(answer=answer)).run([SNEAKERS]) == [None]


async def test_a_candidate_that_was_never_offered_is_ignored():
    """The model may describe products, never add one."""
    answer = LineChecks(
        items=[
            LineCheck(
                index=0,
                candidates=[
                    CandidateCheck(candidate=1, same_product=True),
                    CandidateCheck(candidate=2, same_product=True),
                    CandidateCheck(candidate=9, same_product=True),
                ],
            )
        ]
    )

    [assessed] = await CandidateAssessor(Observing(answer=answer)).run([SNEAKERS])

    assert assessed is not None and len(assessed) == 2


async def test_a_line_index_that_was_never_given_is_ignored():
    answer = LineChecks(items=[LineCheck(index=7, candidates=[])])

    assert await CandidateAssessor(Observing(answer=answer)).run([SNEAKERS]) == [None]


async def test_a_failed_call_leaves_its_lines_unassessed_and_does_not_raise():
    assessor = CandidateAssessor(BrokenLLM(LLMCallError("fake", RuntimeError("down"))))

    assert await assessor.run([SNEAKERS, SNEAKERS]) == [None, None]


async def test_a_line_with_no_candidates_costs_no_call():
    model = Observing(sized)

    assert await CandidateAssessor(model).run([("nothing", [])]) == [[]]
    assert model.calls == 0


async def test_lines_go_in_batches_and_come_back_in_order():
    model = Observing(sized)
    lines = [(f"sneakers {n}", [f"SNEAKERS STEEL TOE {n}CM"]) for n in range(5)]

    assessed = await CandidateAssessor(model, batch=2).run(lines)

    assert model.calls == 3
    assert [one[0].attributes[0].offered for one in assessed] == [f"{n}CM" for n in range(5)]


def test_the_prompt_shows_descriptions_and_never_an_item_code():
    """The model cannot invent a product it is not asked to name."""
    _, human = build_messages([SNEAKERS])

    assert "SNEAKERS STEEL TOE 25CM" in human[1]
    assert "T19036300" not in human[1]


def test_the_prompt_spends_no_words_on_a_candidate_that_is_another_product():
    """A different product scores 0 whatever its properties, and describing
    them anyway is what made one call outrun its timeout."""
    system, _ = build_messages([SNEAKERS])

    assert "NO properties" in system[1]


def test_one_line_per_call_unless_told_otherwise():
    """Twenty lines in one call ran past the timeout on a real RFQ."""
    assert BATCH == 1


def test_the_prompt_points_at_what_the_candidates_differ_in():
    _, human = build_messages([SNEAKERS])

    assert "candidates differ in: 25cm, 29, cm" in human[1]


def test_differences_are_the_words_not_every_candidate_carries():
    shortlist = ["SNEAKERS STEEL TOE 25CM", "SNEAKERS STEEL TOE 29 CM"]

    assert differences(shortlist) == ["25cm", "29", "cm"]
    assert differences(["ONE PRODUCT"]) == [], "one product differs from nothing"


class Crowded:
    """A model that takes a moment per call and remembers the most it had at once."""

    def __init__(self) -> None:
        self.now = 0
        self.most = 0

    async def invoke(self, messages, schema):
        self.now += 1
        self.most = max(self.most, self.now)
        await asyncio.sleep(0.01)
        self.now -= 1
        return LLMResult(value=LineChecks(), model="fake", latency_ms=1)


async def test_every_rfq_shares_one_limit_on_calls_in_flight():
    """Three RFQs at once may not open three times as many calls: the provider's
    rate limit is one limit for the whole service."""
    model = Crowded()
    assessor = CandidateAssessor(model, batch=1, concurrency=2)
    rfq = [(f"line {n}", ["SOMETHING"]) for n in range(4)]

    await asyncio.gather(assessor.run(rfq), assessor.run(rfq), assessor.run(rfq))

    assert model.most == 2

