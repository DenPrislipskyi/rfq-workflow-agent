"""The two ways a matched product gets its confidence, behind one interface."""

from src.domain.rules.catalog import Catalog
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.matching.assessor import CandidateAssessor
from src.services.matching.schemas import CandidateCheck, LineCheck, LineChecks, PropertyCheck
from src.services.matching.scoring import AssessedConfidence, Score, WordCoverage
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
