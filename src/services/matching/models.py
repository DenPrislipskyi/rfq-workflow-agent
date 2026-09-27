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
    """One candidate as it reaches a record: the product, its score, and why.

    The score is 0-100 and absolute, so it reads the same on every line - how
    it is worked out is the `Scorer`'s business (`scoring.py`). `None` where
    the scorer could not score this line; `why` is what the scorer observed,
    empty for a score that has no reason to give.
    """

    item: CatalogItem
    confidence: int | None = None
    why: str = ""


@dataclass(frozen=True, slots=True)
class MatchedLine:
    """What became of one line."""

    index: int
    # The line as the reader got it out of the file. Never touched by anything
    # here - rule 7 of this project, and what makes a wrong match explainable.
    verbatim: str
    # The text the catalogue was actually searched with: the sheet row's own
    # customer wording when a quoted code was overruled, the line's own
    # description when there was no code to go on, and empty when a code was
    # confirmed and nothing had to be searched at all.
    query: str = ""
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
    # The shortlist, each candidate scored against the line - in the search's
    # order, or re-ranked by score where the scorer judges products. Empty for
    # a confirmed code: there was nothing to choose between.
    candidates: list["ScoredItem"] = field(default_factory=list)
    # How much of the line this product accounts for, 0-100. The same score a
    # candidate carries, and on a confirmed code it comes from the same two
    # sentences the judge read. `None` only where nothing was compared: a
    # refusal, or a product a person picked by hand.
    confidence: int | None = None

    @property
    def matched(self) -> bool:
        return self.item_code is not None
