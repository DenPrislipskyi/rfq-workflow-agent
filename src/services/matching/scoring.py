"""How a line's products get their confidence - one interface, two ways.

    AssessedConfidence   a model observes each candidate, `confidence.py`
                         scores what it observed; the shortlist is re-ranked
                         by that score
    WordCoverage         the share of the line's words the product carries;
                         the shortlist stays in the search's order

The pipeline knows only `Scorer`. Which one runs is a setting
(`MATCHING_CONFIDENCE`), so the model-backed one can be switched off without
touching the pipeline, and the pipeline can be tested without a model.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from src.domain.rules.catalog import CatalogItem, tokenize
from src.services.matching.assessor import CandidateAssessor
from src.services.matching.confidence import confidences

# What was asked, and the products to score against it, in shortlist order.
type Question = tuple[str, Sequence[CatalogItem]]

# "65MM" is the number 65 with a unit stuck to it, and the sheet writes the
# same size both ways. Only the word count widens like this; the index does not.
NUMBER_AND_UNIT = re.compile(r"^(\d+(?:[.,]\d+)?)([a-z]{1,4})$")


@dataclass(frozen=True, slots=True)
class Score:
    """One product's confidence for one line, and the reason behind it.

    `None` is "not scored" - the model did not answer for this line - and it
    is shown as a dash, never as a number nobody worked out.
    """

    confidence: int | None
    why: str = ""


class Scorer(Protocol):
    # Whether the shortlist should be re-ordered by the score. Only a score
    # that judges the product is fit to overrule the search's ranking.
    ranks: bool

    async def score(self, questions: Sequence[Question]) -> list[list[Score]]:
        """One list per question, one score per product, in the order given."""
        ...


class AssessedConfidence:
    """A model observes; a formula scores. See `confidence.py`."""

    ranks = True

    def __init__(self, assessor: CandidateAssessor) -> None:
        self._assessor = assessor

    async def score(self, questions: Sequence[Question]) -> list[list[Score]]:
        assessed = await self._assessor.run(
            [(asked, [one.description for one in products]) for asked, products in questions]
        )
        scored: list[list[Score]] = []
        for (_, products), assessments in zip(questions, assessed, strict=True):
            if assessments is None:
                scored.append([Score(None) for _ in products])
                continue
            scored.append(
                [
                    Score(value, one.why)
                    for value, one in zip(confidences(assessments), assessments, strict=True)
                ]
            )
        return scored


class WordCoverage:
    """How much of what we asked with each product carries, 0-100.

    A percentage of the **line's** words, so an item described at length is
    not punished for saying more than it was asked. It cannot tell a variant
    from a match - four sneakers that differ only in size all carry every word
    of `Steel toe sneakers` - which is why it is the fallback and not the
    default, and why it does not re-rank: the search knows which words are
    rare, and counting words does not.
    """

    ranks = False

    async def score(self, questions: Sequence[Question]) -> list[list[Score]]:
        return [
            [Score(covered(words(asked), words(product.description))) for product in products]
            for asked, products in questions
        ]


def words(text: str) -> set[str]:
    """The words of one sentence as the word count counts them: the search's
    own tokenizer, and a number welded to a unit also as the bare number."""
    found: set[str] = set()
    for word in tokenize(text):
        found.add(word)
        if match := NUMBER_AND_UNIT.match(word):
            found.add(match.group(1))
    return found


def covered(asked: set[str], offered: set[str]) -> int:
    """A percentage of the words we asked with, not of the words it has."""
    return round(100 * len(asked & offered) / len(asked)) if asked else 0
