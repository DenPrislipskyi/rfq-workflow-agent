"""How sure we are that a candidate is what the line asked for, 0-100.

The model does not give the number. It reports facts about each candidate -
is it the same kind of product, and for each property that tells products
apart, do the line and the product agree, disagree, or does one of them not
say - and the number is worked out here, by one formula that reads the same
on every line:

    not the same product, or any property in conflict   -> 0
    the same product, nothing to tell variants apart    -> 100
    otherwise   50 for the product itself
              + 50 x the weighted share of its properties we can vouch for

What "vouch for" is worth per property:

    both sides state it, and they agree            1
    the line is silent, the product states it      1 / N, N being how many
                                                   values of it the line's
                                                   candidates offer
    the line states it, the product is silent      1/2

The middle rule is the one this module exists for. `Steel toe sneakers`
against four sneakers that differ only in size is a guess among four, and
each of them scores 50 + 50 x 1/4 = 63 - not 100, which is what counting the
line's words gave them. A size the customer did not name, on a product that
comes in one size only, is no guess at all: N is 1 and it scores 100.

A model's own "85%" would be a word that looks like a number. These are
arithmetic over what it observed, so two lines with the same facts get the
same score, and the score can be explained property by property.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

# What the product itself is worth, before any of its properties: a candidate
# of the right kind is at least half an answer.
PRODUCT_SHARE = 50
# A property that decides which product this is - size, model, rating - weighs
# three times one that only describes it - brand, packaging, finish.
KEY_WEIGHT = 3
MINOR_WEIGHT = 1
# A property the customer named and the product does not mention: nothing
# says yes, nothing says no.
UNCONFIRMED_CREDIT = 0.5


class Agreement(StrEnum):
    """What one property looks like from both sides."""

    MATCH = "match"
    CONFLICT = "conflict"
    # The line does not say; the product does.
    UNSTATED = "unstated"
    # The line says; the product does not.
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class Attribute:
    """One property of one candidate, as the line and the product state it."""

    name: str
    key: bool
    agreement: Agreement
    # The product's value. It is what tells one variant from another, so it is
    # what the variants are counted by.
    offered: str = ""


@dataclass(frozen=True, slots=True)
class Assessment:
    """Everything that was observed about one candidate for one line."""

    same_product: bool
    attributes: tuple[Attribute, ...] = ()
    why: str = ""


def confidences(assessments: Sequence[Assessment]) -> list[int]:
    """The score of every candidate of one line, in the order given.

    Scored together rather than one by one: "a guess among four" is a fact
    about the four, and no candidate can know it alone.
    """
    variants = _variants(assessments)
    return [_score(one, variants) for one in assessments]


def _score(assessment: Assessment, variants: dict[str, int]) -> int:
    if not _viable(assessment):
        return 0
    if not assessment.attributes:
        return 100
    total = sum(_weight(one) for one in assessment.attributes)
    earned = sum(_weight(one) * _credit(one, variants) for one in assessment.attributes)
    return _rounded(PRODUCT_SHARE + (100 - PRODUCT_SHARE) * earned / total)


def _viable(assessment: Assessment) -> bool:
    """The right kind of product, and nothing it states contradicts the line."""
    return assessment.same_product and all(
        one.agreement is not Agreement.CONFLICT for one in assessment.attributes
    )


def _variants(assessments: Sequence[Assessment]) -> dict[str, int]:
    """For each property the line leaves open, how many values its candidates
    offer. Only candidates still in the running count: a product ruled out
    is not one of the choices."""
    offered: dict[str, set[str]] = {}
    for assessment in assessments:
        if not _viable(assessment):
            continue
        for one in assessment.attributes:
            if one.agreement is Agreement.UNSTATED:
                offered.setdefault(_normal(one.name), set()).add(_normal(one.offered))
    return {name: len(values) for name, values in offered.items()}


def _credit(attribute: Attribute, variants: dict[str, int]) -> float:
    match attribute.agreement:
        case Agreement.MATCH:
            return 1.0
        case Agreement.UNSTATED:
            return 1 / max(1, variants.get(_normal(attribute.name), 1))
        case Agreement.UNCONFIRMED:
            return UNCONFIRMED_CREDIT
        case Agreement.CONFLICT:
            return 0.0


def _weight(attribute: Attribute) -> int:
    return KEY_WEIGHT if attribute.key else MINOR_WEIGHT


def _normal(text: str) -> str:
    """`25 CM` and `25cm` are one value, `Size` and `size` one property."""
    return "".join(text.lower().split())


def _rounded(value: float) -> int:
    """Half up, as a person rounds: 62.5 is 63. Python's `round` would make it
    62 - it rounds halves to even - and the score would disagree with anyone
    checking it by hand."""
    return math.floor(value + 0.5)
