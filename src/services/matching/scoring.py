"""How a line's products get their confidence - one interface, two ways.

    AssessedConfidence   a model observes each candidate, `confidence.py`
                         scores what it observed; the shortlist is re-ranked
                         by that score. A line whose own description is a
                         candidate's word for word needs no model at all
    WordCoverage         the share of the line's words the product carries;
                         the shortlist stays in the search's order

The pipeline knows only `Scorer`. Which one runs is a setting
(`MATCHING_CONFIDENCE`), so the model-backed one can be switched off without
touching the pipeline, and the pipeline can be tested without a model.
"""

import re
import unicodedata
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

# What stands beside a candidate whose line was settled word for word.
WORD_FOR_WORD = "The line's own description, word for word."
# ...and beside the others on that line. Not a zero: nobody looked at them,
# and a zero would read as "certainly not".
NOT_ASSESSED = "Not assessed: another candidate matches the line word for word."

# A word, or any single sign that is not one. Signs are kept one by one so
# that `±2 KG` is not `2 KG` and `20%` is not `20`.
_TOKEN = re.compile(r"\w+|[^\w\s]")
# A hyphen or dash that joins two words: `NON-WATERTIGHT` is `NON WATERTIGHT`.
# One in front of a number is a minus and stays: `-20°C` is not `20°C`.
_JOINING_DASH = re.compile(r"(?<=\w)[-\u2010\u2011\u2012\u2013\u2014](?=\w)")
# Signs that only lay the words out and never change what is meant.
_COSMETIC = frozenset(",.;:()[]{}|/\\_'\"`\u201c\u201d\u2018\u2019\u00ab\u00bb")


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
        """Lines settled word for word first, without a call; the model for the rest."""
        asked = [index for index, question in enumerate(questions) if not _settled(question)]
        assessed = dict(
            zip(
                asked,
                await self._assessor.run(
                    [
                        (questions[index][0], [one.description for one in questions[index][1]])
                        for index in asked
                    ]
                ),
                strict=True,
            )
        )
        return [
            _assessed(products, assessed[index])
            if index in assessed
            else _word_for_word(line, products)
            for index, (line, products) in enumerate(questions)
        ]


def _settled(question: Question) -> bool:
    """Whether some candidate is the line's own description, word for word."""
    line, products = question
    return any(same_words(line, product.description) for product in products)


def _word_for_word(line: str, products: Sequence[CatalogItem]) -> list[Score]:
    return [
        Score(100, WORD_FOR_WORD)
        if same_words(line, one.description)
        else Score(None, NOT_ASSESSED)
        for one in products
    ]


def _assessed(products: Sequence[CatalogItem], assessments) -> list[Score]:
    if assessments is None:
        return [Score(None) for _ in products]
    return [
        Score(value, one.why)
        for value, one in zip(confidences(assessments), assessments, strict=True)
    ]


def same_words(line: str, description: str) -> bool:
    """The same words in the same order, and nothing else differs.

    Only what lays the words out is set aside: case, spacing, commas, stops,
    brackets, quotes, slashes, a hyphen between words, and the typographic
    variants of a character. A sign that means something - `±`, `%`, `+`, `°`,
    `*`, a minus - must match like a word. Nothing is stemmed, dropped or
    reordered: `Weldings gloves(five fingers)` against `WELDER GLOVES FIVE
    FINGERS` is two words apart and goes to the model, and so does `25CM`
    against `25 CM`. Anything short of word for word is exactly the case where
    a person can be wrong about what was meant.
    """
    words = _normal_words(line)
    return bool(words) and words == _normal_words(description)


def _normal_words(text: str) -> list[str]:
    joined = _JOINING_DASH.sub(" ", unicodedata.normalize("NFKC", text).casefold())
    return [token for token in _TOKEN.findall(joined) if token not in _COSMETIC]


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
