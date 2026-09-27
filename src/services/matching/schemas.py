"""What the model is allowed to answer with when judging a row of the sheet.

Indexed rather than positional: a model that drops or merges an entry in a list
of fifty silently shifts every judgement after it, and a shifted judgement
confirms somebody else's product. The index makes that detectable instead.
"""

from pydantic import BaseModel, Field


class Judgement(BaseModel):
    """Whether one row's two descriptions are about one product."""

    # The number the prompt gave this pair. Not a row number of the RFQ and not
    # a row number of the sheet - the three are kept apart on purpose, so that
    # a renumbering anywhere cannot quietly re-point an answer.
    index: int
    same: bool
    # One sentence, and it goes into the record beside the outcome. This is what
    # an operator reads when they want to know why a quoted code was dropped.
    why: str = ""


class Judgements(BaseModel):
    items: list[Judgement] = Field(default_factory=list)


# --- assessing a shortlist ---------------------------------------------------
#
# The model reports what it read on each side and whether two stated values
# agree. Which of the four agreements that makes - match, conflict, unstated,
# unconfirmed - is derived from those facts in code, not asked for: a model
# asked to name a category has one more way to be wrong than a model asked
# what the two sentences say.


class PropertyCheck(BaseModel):
    """One property that tells products of this kind apart."""

    # Short and in lower case - `size`, `colour`, `brand`, `model` - and the
    # same name for the same property on every candidate of a line, because
    # variants are counted by it.
    name: str
    # True for a property that decides which product this is (size, model,
    # rating, capacity); false for one that only describes it (brand,
    # packaging, finish).
    key: bool = True
    # Each side's value as written, empty where that side does not state it.
    line_value: str = ""
    item_value: str = ""
    # Only read when both sides state a value: whether they are the same value,
    # however written (`M14 X 50MM` and `M14*50` are).
    agrees: bool = False


class CandidateCheck(BaseModel):
    """What was observed about one candidate for one line."""

    # The number the prompt gave this candidate within its line.
    candidate: int
    same_product: bool
    properties: list[PropertyCheck] = Field(default_factory=list)
    # One sentence an operator reads beside the score.
    why: str = ""


class LineCheck(BaseModel):
    # The number the prompt gave this line, as in `Judgement.index`.
    index: int
    candidates: list[CandidateCheck] = Field(default_factory=list)


class LineChecks(BaseModel):
    items: list[LineCheck] = Field(default_factory=list)
