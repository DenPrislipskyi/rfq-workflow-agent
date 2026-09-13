"""The lines of an RFQ against our own product list.

    restate the lines          1 model call per ~50 lines
    look each one up           no model

One model call for an RFQ of any size, and it never sees the catalogue: it only
says the customer's line back in the vocabulary the sheet uses, so that the two
can be compared. Everything after that is arithmetic over words.

The desk's specification, in three branches:

    the customer's code names an item, and our restated line agrees
    with that item's own customer wording          -> that item, and nothing else
    the code names an item and the wordings differ -> drop it, search, top five
    the code names nothing, or there was no code   -> search, top five

Only the last two produce a shortlist. A confirmed code produces one product,
because there was nothing to choose between.
"""

import logging
import re
from collections.abc import Sequence

from src.domain.rules.catalog import Candidate, Catalog, CatalogItem, tokenize
from src.services.extraction.models import LineItem
from src.services.matching.describe import LineDescriber
from src.services.matching.models import (
    BY_SEARCH,
    CODE_CONFIRMED,
    CODE_REJECTED,
    NOTHING,
    MatchedLine,
    ScoredItem,
)

logger = logging.getLogger(__name__)

NO_CATALOGUE = "There is no catalogue to match against."
NOTHING_FOUND = "The catalogue had nothing to offer for this line."
CONFIRMED = "The customer's code names this product and the descriptions agree ({percent}%)."
REJECTED = (
    "The customer's code names {code}, whose description agrees only {percent}% - "
    "rejected, and the catalogue searched by description instead."
)
SEARCHED = "No item carries this customer code, so the catalogue was searched by description."
NO_CODE = "The customer quoted no code, so the catalogue was searched by description."

# How much of a line has to appear in an item's own customer wording before the
# code that named it is taken at its word. The desk's number.
AGREEMENT = 80.0

# "65MM" is the number 65 with a unit stuck to it, and the sheet writes the same
# size both ways. Only the comparison widens like this; the index does not.
NUMBER_AND_UNIT = re.compile(r"^(\d+(?:[.,]\d+)?)([a-z]{1,4})$")


class MatchingPipeline:
    """Turns the lines of one RFQ into products, or into reasons there are none."""

    def __init__(
        self,
        describer: LineDescriber,
        *,
        candidates: int = 5,
        agreement: float = AGREEMENT,
    ) -> None:
        self._describer = describer
        # How many products a line that had to be searched is narrowed down to.
        self._candidates = candidates
        # The threshold a confirmed code has to clear, as a percentage.
        self._agreement = agreement

    async def run(self, items: Sequence[LineItem], catalog: Catalog) -> list[MatchedLine]:
        """Every line, matched or refused, in the order they arrived.

        Never raises and never drops a line: an RFQ of fifteen items comes back
        as fifteen answers, and "no" is one of them.
        """
        if not items:
            return []
        if not len(catalog):
            logger.warning("No catalogue loaded - nothing can be matched")
            return [_unmatched(item, NO_CATALOGUE) for item in items]

        wordings = [item.description or "" for item in items]
        described = await self._describer.run(wordings)

        matched = [
            self._one(item, description, catalog)
            for item, description in zip(items, described, strict=True)
        ]
        _log(matched)
        return matched

    def _one(self, item: LineItem, description: str, catalog: Catalog) -> MatchedLine:
        """One line down one of the three branches."""
        found = catalog.by_code(item.customer_item_code)

        if found is not None:
            agreement = _agreement(
                item.description or "", description, found.customer_description
            )
            if agreement > self._agreement:
                return _line(
                    item,
                    description,
                    item_code=found.code,
                    product=found,
                    how=CODE_CONFIRMED,
                    why=CONFIRMED.format(percent=round(agreement)),
                    confidence=100,
                )
            return _line(
                item,
                description,
                how=CODE_REJECTED,
                why=REJECTED.format(code=found.code, percent=round(agreement)),
                candidates=catalog.search(description, limit=self._candidates),
            )

        ranked = catalog.search(description, limit=self._candidates)
        if not ranked:
            return _line(item, description, how=NOTHING, why=NOTHING_FOUND)
        return _line(
            item,
            description,
            how=BY_SEARCH,
            why=SEARCHED if item.customer_item_code else NO_CODE,
            candidates=ranked,
        )


def _agreement(verbatim: str, description: str, customer_description: str) -> float:
    """How much of a line the item's own customer wording carries.

    Measured twice - once against the customer's own sentence, once against our
    restatement of it - and the better of the two stands.

    The customer's own sentence is the comparison that belongs here. The column
    it is measured against holds *a customer's* wording, so a line that says
    what the sheet already says must score full marks; it did not, because we
    were comparing our restatement to their sentence and calling the difference
    disagreement. `RULE CONVEX` against `Convex rulers` scored 50% and the code
    was dropped, for a line that matched the sheet word for word.

    The restatement stays as the second chance, for the line that means the
    same thing in different words. Whichever agrees more is the answer.
    """
    theirs = _compared(customer_description)
    return max(
        _overlap(_compared(verbatim), theirs),
        _overlap(_compared(description), theirs),
    )


def _overlap(ours: Sequence[str] | set[str], theirs: set[str]) -> float:
    """A percentage of the words we asked with, not of the words it has.

    An item described at length is not punished for saying more than was asked,
    and a line whose every word is there is the same product however much the
    sheet goes on about it.
    """
    ours = set(ours)
    return 100.0 * len(ours & theirs) / len(ours) if ours else 0.0


def _compared(text: str) -> set[str]:
    """The words of one sentence, as this comparison counts them.

    The search's own tokenizer, and then one thing more: a number welded to a
    unit also counts as the bare number, so `65MM` and `65` are not two
    different sizes.

    This widening is for the comparison only. The catalogue is built and
    searched exactly as it was - nothing here reaches the index, and the
    ranking that `test_cases_2/3` gets right is untouched.
    """
    words: set[str] = set()
    for word in tokenize(text):
        words.add(word)
        if match := NUMBER_AND_UNIT.match(word):
            words.add(match.group(1))
    return words


def _scored(candidates: Sequence[Candidate]) -> list[ScoredItem]:
    """The search's own ranking, as a percentage of its best row.

    Relative on purpose, and only within one line: a BM25 score is not a
    probability and two lines' scores are not comparable. What it does say -
    and all it says - is how far each candidate is behind the one above it.
    """
    if not candidates:
        return []
    top = candidates[0].score or 1.0
    return [
        ScoredItem(
            item=candidate.item,
            confidence=max(0, min(100, round(100.0 * candidate.score / top))),
        )
        for candidate in candidates
    ]


def _line(
    item: LineItem,
    description: str,
    *,
    item_code: str | None = None,
    product: CatalogItem | None = None,
    how: str,
    why: str,
    confidence: int | None = None,
    candidates: Sequence[Candidate] = (),
) -> MatchedLine:
    return MatchedLine(
        index=item.sr_no,
        verbatim=item.description or "",
        description=description,
        customer_code=item.customer_item_code,
        quantity=item.quantity,
        uom=item.uom,
        item_code=item_code,
        item=product,
        how=how,
        why=why,
        confidence=confidence,
        candidates=_scored(candidates),
    )


def _unmatched(item: LineItem, why: str) -> MatchedLine:
    return MatchedLine(
        index=item.sr_no,
        verbatim=item.description or "",
        description=item.description or "",
        customer_code=item.customer_item_code,
        quantity=item.quantity,
        uom=item.uom,
        why=why,
    )


def _log(matched: Sequence[MatchedLine]) -> None:
    """One line for a run of any size. The counts are what a reader scans for."""
    logger.info(
        "Matched | %d of %d line(s) confirmed by code | %d searched | %d code(s) overruled",
        sum(1 for line in matched if line.how == CODE_CONFIRMED),
        len(matched),
        sum(1 for line in matched if line.how == BY_SEARCH),
        sum(1 for line in matched if line.how == CODE_REJECTED),
    )
