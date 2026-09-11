"""What one line of an RFQ looks like on its way through matching."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from src.domain.rules.catalog import CatalogItem

# How a line ended up with the product it did. Computed by code from what the
# model answered, never asked of the model: it is a fact about which route
# produced the code, and the model does not know which route it was shown.
CODE_CONFIRMED = "code_confirmed"
CODE_REJECTED = "code_rejected"
BY_SEARCH = "search"
NOTHING = "none"


@dataclass(frozen=True, slots=True)
class Question:
    """One line, and everything the catalogue could offer for it."""

    index: int
    # What we asked the catalogue with: the line restated in our own words.
    description: str
    # What the customer actually wrote. Shown to the model as well when it
    # differs, because a restatement can lose something the original kept.
    verbatim: str
    candidates: Sequence[CatalogItem] = ()
    # The code of the candidate the customer's own code led to, if any.
    by_code: str | None = None


@dataclass(frozen=True, slots=True)
class Choice:
    """What the model said about one line, after it has been checked."""

    item_code: str | None
    why: str
    # How sure it was of each candidate, by item code. Only for codes it was
    # actually shown - a score for anything else is dropped with the answer.
    scores: dict[str, int] = field(default_factory=dict)

    @property
    def confidence(self) -> int | None:
        """How sure it was of the one it picked."""
        return self.scores.get(self.item_code) if self.item_code else None


@dataclass(frozen=True, slots=True)
class ScoredItem:
    """One candidate as it reaches a record: the product, and the score."""

    item: CatalogItem
    confidence: int = 0


@dataclass(frozen=True, slots=True)
class MatchedLine:
    """What became of one line."""

    index: int
    verbatim: str
    description: str
    customer_code: str | None = None
    # As the customer wrote them. The catalogue has units of its own and they
    # are not these: converting one into the other is an operator's job.
    quantity: str | None = None
    uom: str | None = None
    item_code: str | None = None
    # The product itself, with every column of its row. None for a refusal.
    item: CatalogItem | None = None
    how: str = NOTHING
    why: str = ""
    # Every product the model was shown for this line, in the order the search
    # ranked them, each with the score the model gave it.
    candidates: list["ScoredItem"] = field(default_factory=list)
    confidence: int | None = None

    @property
    def matched(self) -> bool:
        return self.item_code is not None
