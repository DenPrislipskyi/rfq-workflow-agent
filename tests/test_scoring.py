"""The two ways a matched product gets its confidence, behind one interface."""

from src.domain.rules.catalog import Catalog
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.matching.assessor import CandidateAssessor
from src.services.matching.schemas import CandidateCheck, LineCheck, LineChecks, PropertyCheck
import pytest

from src.services.matching.scoring import (
    NOT_ASSESSED,
    WORD_FOR_WORD,
    AssessedConfidence,
    Score,
    WordCoverage,
    same_words,
)
from tests.fakes import BrokenLLM, FakeLLM

SNEAKERS = ["SNEAKERS STEEL TOE 25CM", "SNEAKERS STEEL TOE 29 CM"]


def products(*descriptions: str):
    sheet = Catalog.from_rows(
        [{"Item Code": f"T{n}", "Item Description": text} for n, text in enumerate(descriptions)],
        code_column="Item Code",
        description_column="Item Description",
    )
    return [sheet.by_code(f"T{n}") for n in range(len(descriptions))]


def sizes_left_open(*values: str) -> LineChecks:
    return LineChecks(
        items=[
            LineCheck(
                index=0,
                candidates=[
                    CandidateCheck(
                        candidate=number,
                        same_product=True,
                        properties=[PropertyCheck(name="size", item_value=value)],
                        why="size not stated",
                    )
                    for number, value in enumerate(values, start=1)
                ],
            )
        ]
    )


async def test_assessed_confidence_scores_what_the_model_observed():
    scorer = AssessedConfidence(CandidateAssessor(FakeLLM(sizes_left_open("25CM", "29 CM"))))

    [scored] = await scorer.score([("Steel toe sneakers", products(*SNEAKERS))])

    assert scored == [Score(75, "size not stated"), Score(75, "size not stated")]


async def test_assessed_confidence_leaves_an_unanswered_line_unscored():
    """A dash, not a number nobody worked out."""
    broken = BrokenLLM(LLMCallError("fake", RuntimeError("down")))
    scorer = AssessedConfidence(CandidateAssessor(broken))

    [scored] = await scorer.score([("Steel toe sneakers", products(*SNEAKERS))])

    assert scored == [Score(None), Score(None)]


async def test_word_coverage_counts_the_line_s_words_and_cannot_tell_variants_apart():
    """Why it is the fallback: every sneaker carries all three words."""
    [scored] = await WordCoverage().score([("Steel toe sneakers", products(*SNEAKERS))])

    assert [one.confidence for one in scored] == [100, 100]


async def test_only_the_score_that_judges_products_reranks_them():
    assert AssessedConfidence.ranks is True
    assert WordCoverage.ranks is False


async def test_no_questions_no_scores():
    assert await WordCoverage().score([]) == []
    assert await AssessedConfidence(CandidateAssessor(FakeLLM(LineChecks()))).score([]) == []


# --- word for word: no model needed ----------------------------------------


class Counting:
    """A model that answers every call with the same observations, and counts."""

    def __init__(self, answer: LineChecks) -> None:
        self._answer = answer
        self.prompts: list[str] = []

    async def invoke(self, messages, schema):
        self.prompts.append(messages[-1][1])
        return await FakeLLM(self._answer).invoke(messages, schema)


async def test_a_line_that_is_a_candidate_word_for_word_costs_no_call():
    """RFQ-0025: `BACON SMOKED WHOLE` against `BACON SMOKED WHOLE`."""
    model = Counting(LineChecks())
    scorer = AssessedConfidence(CandidateAssessor(model))

    [scored] = await scorer.score(
        [("BACON SMOKED WHOLE", products("BACON SMOKED WHOLE", "SMOKED TURKEY ROLL"))]
    )

    assert model.prompts == []
    assert scored == [Score(100, WORD_FOR_WORD), Score(None, NOT_ASSESSED)]


async def test_a_confirmed_code_that_reads_word_for_word_costs_no_call_either():
    model = Counting(LineChecks())

    [scored] = await AssessedConfidence(CandidateAssessor(model)).score(
        [("Milano salami", products("MILANO SALAMI"))]
    )

    assert model.prompts == []
    assert scored == [Score(100, WORD_FOR_WORD)]


async def test_only_the_lines_that_are_not_word_for_word_go_to_the_model():
    model = Counting(sizes_left_open("25CM", "29 CM"))
    scorer = AssessedConfidence(CandidateAssessor(model))

    exact, sneakers = await scorer.score(
        [
            ("BACON SMOKED WHOLE", products("BACON SMOKED WHOLE")),
            ("Steel toe sneakers", products(*SNEAKERS)),
        ]
    )

    assert len(model.prompts) == 1
    assert "Steel toe sneakers" in model.prompts[0]
    assert "BACON" not in model.prompts[0]
    assert exact == [Score(100, WORD_FOR_WORD)]
    assert [one.confidence for one in sneakers] == [75, 75]


@pytest.mark.parametrize(
    ("line", "description"),
    [
        ("BACON SMOKED WHOLE", "bacon smoked whole"),
        ("SOUP SEASONING (VEGETA) PODRAVKA", "soup seasoning vegeta podravka"),
        ("NON-WATERTIGHT CONNECTOR “SIEMENS”", 'NON WATERTIGHT CONNECTOR "SIEMENS"'),
        ("HEX HEAD BOLT/NUT", "HEX HEAD BOLT NUT"),
        ("ITALIAN MORTADELLE, ±2 KG/PC", "italian mortadelle ±2 kg pc"),
    ],
)
def test_case_spacing_and_layout_do_not_matter(line, description):
    assert same_words(line, description)


@pytest.mark.parametrize(
    ("line", "description"),
    [
        ("Steel toe sneakers", "SNEAKERS STEEL TOE"),
        ("bacon smoked", "BACON SMOKED WHOLE"),
        ("Torch light | RefNo.GFL3801N", "Torch light | GFL3801N"),
        ("Weldings gloves(five fingers)", "WELDER GLOVES FIVE FINGERS"),
        ("25CM", "25 CM"),
        ("0.5", "05"),
        ("M8*50", "M8 50"),
        ("±2 KG", "2 KG"),
        ("20%", "20"),
        ("-20°C", "20°C"),
        ("", ""),
    ],
)
def test_anything_short_of_word_for_word_is_not(line, description):
    """Another word, another order, a sign that means something, or nothing at all."""
    assert not same_words(line, description)

