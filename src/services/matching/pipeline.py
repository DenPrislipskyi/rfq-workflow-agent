"""A to D, joined: the lines of an RFQ against our own product list.

    restate the lines          1 model call per ~50 lines
    look each one up           no model: by code, then by words
    choose, or refuse          1 model call per ~20 lines

Two calls for an RFQ of any size, and neither of them ever sees the catalogue -
only the handful of products the search put in front of it. That is not an
optimisation: a catalogue does not fit in a prompt at any size, and the day it
did would be the day it stopped being worth searching.

Where a customer quoted a code, the product that code names leads the shortlist
and is marked as such. It is still only a candidate. The desk's own note is
that a code cannot be trusted on its own, and the two examples they sent are
both codes that lead somewhere else - so what settles it is the description,
every time.
"""

import logging
from collections.abc import Sequence

from src.domain.rules.catalog import Catalog, CatalogItem, Shortlist
from src.services.extraction.models import LineItem
from src.services.matching.decide import ItemChooser
from src.services.matching.describe import LineDescriber
from src.services.matching.models import (
    BY_SEARCH,
    CODE_CONFIRMED,
    CODE_REJECTED,
    NOTHING,
    Choice,
    MatchedLine,
    Question,
    ScoredItem,
)

logger = logging.getLogger(__name__)

NO_CATALOGUE = "There is no catalogue to match against."
NOTHING_TO_MATCH = "The line says nothing that could be matched."


class MatchingPipeline:
    """Turns the lines of one RFQ into products, or into reasons there are none."""

    def __init__(
        self,
        describer: LineDescriber,
        chooser: ItemChooser,
        *,
        candidates: int = 5,
    ) -> None:
        self._describer = describer
        self._chooser = chooser
        # How many products one line is narrowed down to. Measured rather than
        # chosen: recall stops improving past five on this catalogue, and every
        # candidate past that is prompt spent on an option nobody picks.
        self._candidates = candidates

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

        shortlists = [
            catalog.shortlist(
                code=item.customer_item_code,
                description=description,
                limit=self._candidates,
            )
            for item, description in zip(items, described, strict=True)
        ]

        questions = [
            Question(
                index=index,
                description=description,
                verbatim=item.description or "",
                candidates=[candidate.item for candidate in shortlist.candidates],
                by_code=shortlist.by_code.code if shortlist.by_code else None,
            )
            for index, (item, description, shortlist) in enumerate(
                zip(items, described, shortlists, strict=True)
            )
        ]

        chosen = await self._chooser.run(questions)

        matched = [
            _matched(item, description, shortlist, chosen.get(index, Choice(None, "")))
            for index, (item, description, shortlist) in enumerate(
                zip(items, described, shortlists, strict=True)
            )
        ]
        _log(matched)
        return matched


def _matched(
    item: LineItem, description: str, shortlist: Shortlist, choice: Choice
) -> MatchedLine:
    return MatchedLine(
        index=item.sr_no,
        verbatim=item.description or "",
        description=description,
        customer_code=item.customer_item_code,
        quantity=item.quantity,
        uom=item.uom,
        item_code=choice.item_code,
        item=_product(shortlist, choice.item_code),
        how=_how(shortlist, choice.item_code),
        why=choice.why,
        confidence=choice.confidence,
        candidates=[
            ScoredItem(item=candidate.item, confidence=choice.scores.get(candidate.item.code, 0))
            for candidate in shortlist.candidates
        ],
    )


def _product(shortlist: Shortlist, item_code: str | None) -> CatalogItem | None:
    """The candidate the model chose. Always one of the ones it was shown -
    `ItemChooser` has already refused anything that was not."""
    if item_code is None:
        return None
    return next(
        (one.item for one in shortlist.candidates if one.item.code == item_code), None
    )


def _how(shortlist: Shortlist, item_code: str | None) -> str:
    """Which route produced this product.

    Read off what happened rather than asked of the model: the model is shown a
    list and does not know which of its entries came from a code. It is also
    the one field an operator looks at first - "the customer's code was wrong"
    and "we found this by its words" are different things to be told.
    """
    if item_code is None:
        return NOTHING
    if shortlist.by_code is None:
        return BY_SEARCH
    return CODE_CONFIRMED if shortlist.by_code.code == item_code else CODE_REJECTED


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
    rejected = sum(1 for line in matched if line.how == CODE_REJECTED)
    logger.info(
        "Matched | %d of %d line(s) | %d by code, %d by words | %d code(s) overruled",
        sum(1 for line in matched if line.matched),
        len(matched),
        sum(1 for line in matched if line.how == CODE_CONFIRMED),
        sum(1 for line in matched if line.how == BY_SEARCH),
        rejected,
    )
