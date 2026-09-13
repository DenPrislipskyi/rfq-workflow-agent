"""What one line of an RFQ looks like on its way through matching."""

from dataclasses import dataclass, field

from src.domain.rules.catalog import CatalogItem

# How a line ended up with the product it did, or with none. A fact about
# which of the three branches produced it, computed where it happened.
CODE_CONFIRMED = "code_confirmed"
CODE_REJECTED = "code_rejected"
BY_SEARCH = "search"
NOTHING = "none"


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
    # The shortlist, in the order the search ranked it, each scored against
    # the best of its own line. Empty for a confirmed code: there was nothing
    # to choose between.
    candidates: list["ScoredItem"] = field(default_factory=list)
    confidence: int | None = None

    @property
    def matched(self) -> bool:
        return self.item_code is not None
