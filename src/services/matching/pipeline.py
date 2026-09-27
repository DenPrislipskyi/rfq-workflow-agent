"""The lines of an RFQ against our own product list.

    judge what a code led to    1 model call per 50 lines that had one
    look each line up           no model

The desk's specification, in three branches:

    the code names a product, and it is what this line asked for
                                        -> that row, whole, and nothing else
    the code names a product that is not what this line asked for
                                        -> drop the code, search the sheet with
                                           the line's own words, top five
    the code names nothing, or there was no code
                                        -> search the sheet with the line's own
                                           words, top five

**What is judged is the customer's own line against the product their code
leads to.** A code is a claim about a product and the words beside it are
another; where they disagree, the words win, because the words are what the
customer is actually asking for. `110188` in today's sheet names a fishing rod,
and the line that quotes it asks for a 4TB hard drive.

Every search reads the `Item Description / SSG Description` column, and every
search is asked with the line's own words - the same question in both branches
that have one, because it is the same question.

Only the last two produce a shortlist. A confirmed code produces one product,
because there was nothing to choose between.

Every product that comes out - the confirmed one, or each on a shortlist - is
then scored against the line by the one `Scorer` the pipeline was given, all
lines of the RFQ in one go (`scoring.py`).
"""

import dataclasses
import logging
from collections.abc import Sequence

from src.domain.rules.catalog import Candidate, Catalog, CatalogItem
from src.services.extraction.models import LineItem
from src.services.matching.judge import AgreementJudge
from src.services.matching.models import (
    BY_SEARCH,
    CODE_CONFIRMED,
    CODE_REJECTED,
    NOTHING,
    MatchedLine,
    ScoredItem,
)
from src.services.matching.scoring import Score, Scorer, WordCoverage

logger = logging.getLogger(__name__)

NO_CATALOGUE = "There is no catalogue to match against."
NOTHING_FOUND = "The sheet had nothing to offer for this line."
CONFIRMED = "The customer's code names this product, and it is what the line asked for."
REJECTED = (
    "The customer's code names {code}, which is not what this line asked for - "
    "the code was dropped and the sheet searched by the line's own words instead."
)
SEARCHED = "No row carries this customer code, so the sheet was searched by the requested description."
NO_CODE = "The customer quoted no code, so the sheet was searched by the requested description."

# What the judge is asked about: the customer's line, and the product their
# code led to. Not two columns of one sheet row - the sheet's own wording for a
# product answers a question nobody asked.
Pair = tuple[str, str]


class MatchingPipeline:
    """Turns the lines of one RFQ into products, or into reasons there are none."""

    def __init__(
        self, judge: AgreementJudge, *, scorer: Scorer | None = None, candidates: int = 5
    ) -> None:
        self._judge = judge
        # Counting words needs no model, so it is what a pipeline built without
        # a scorer gets. Production passes the model-backed one.
        self._scorer = scorer or WordCoverage()
        # How many products a line that had to be searched is narrowed down to.
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

        rows = [catalog.by_code(item.customer_item_code) for item in items]
        verdicts = await self._judged(items, rows)

        unscored = [
            self._one(item, row, verdicts.get(_pair(item, row)), catalog)
            for item, row in zip(items, rows, strict=True)
        ]
        scores = await self._scorer.score([(line.verbatim, _products(line)) for line in unscored])
        matched = [
            _scored(line, scored, ranks=self._scorer.ranks)
            for line, scored in zip(unscored, scores, strict=True)
        ]
        _log(matched)
        return matched

    async def _judged(
        self, items: Sequence[LineItem], rows: Sequence[CatalogItem | None]
    ) -> dict[Pair, tuple[bool, str]]:
        """Every distinct question, asked once.

        Distinct by the pair of sentences rather than by the code: two lines
        quoting one code in the same words ask the same question, and two lines
        quoting it in different words do not - the second is exactly the case
        where one of them is right and the other is not.
        """
        pairs = list(
            dict.fromkeys(
                _pair(item, row)
                for item, row in zip(items, rows, strict=True)
                if row is not None
            )
        )
        return dict(zip(pairs, await self._judge.run(pairs), strict=True))

    def _one(
        self,
        item: LineItem,
        row: CatalogItem | None,
        verdict: tuple[bool, str] | None,
        catalog: Catalog,
    ) -> MatchedLine:
        """One line down one of the three branches."""
        if row is None:
            return self._searched(
                item,
                item.description or "",
                SEARCHED if item.customer_item_code else NO_CODE,
                catalog,
            )

        # A row nobody judged is a row nobody vouched for, and it takes the
        # same branch as one judged against: whatever else happens, a code
        # this pipeline did not check does not reach an order.
        same, why = verdict or (False, "")
        if same:
            # Scored afterwards like any candidate, as a shortlist of one. The
            # score is not what confirmed the code - the judge did that - but it
            # says how much of the line the product accounts for, and a
            # confirmation at 56% is one worth opening.
            return _line(
                item,
                query="",
                item_code=row.code,
                product=row,
                how=CODE_CONFIRMED,
                why=_and(CONFIRMED, why),
            )

        # The same query as the branch below: the customer's own words. Their
        # code has just been shown to name something else, so the sheet's
        # wording for that something else is not what to look for.
        return self._searched(
            item,
            item.description or "",
            _and(REJECTED.format(code=row.code), why),
            catalog,
            how=CODE_REJECTED,
        )

    def _searched(
        self,
        item: LineItem,
        query: str,
        why: str,
        catalog: Catalog,
        *,
        how: str = BY_SEARCH,
    ) -> MatchedLine:
        """The two branches that end in a shortlist, and what happens to an
        empty one: it is a refusal with its reason kept, not a silent match."""
        ranked = catalog.search(query, limit=self._candidates)
        if not ranked:
            return _line(item, query=query, how=NOTHING, why=_and(why, NOTHING_FOUND))
        return _line(item, query=query, how=how, why=why, candidates=ranked)


def _pair(item: LineItem, row: CatalogItem | None) -> Pair:
    """The two sentences the judge is asked about.

    The customer's own line, and the product their code led to. Which line
    asked matters: the same code quoted beside two different descriptions is
    two different questions, and usually one of the two is wrong.
    """
    return ("", "") if row is None else (item.description or "", row.description)


def _and(reason: str, said: str) -> str:
    """The branch's own reason, and what the model said about the row."""
    return f"{reason} {said}".strip() if said else reason


def _products(line: MatchedLine) -> list[CatalogItem]:
    """What gets scored for a line: its confirmed product, or its shortlist."""
    if line.how == CODE_CONFIRMED and line.item is not None:
        return [line.item]
    return [one.item for one in line.candidates]


def _scored(line: MatchedLine, scores: Sequence[Score], *, ranks: bool) -> MatchedLine:
    """The line with its scores in place.

    A shortlist is re-ordered by score only when the scorer judges products;
    ties, and every unscored candidate, keep the search's order - the sort is
    stable, and unscored ones go last.
    """
    if line.how == CODE_CONFIRMED:
        return dataclasses.replace(line, confidence=scores[0].confidence if scores else None)
    candidates = [
        ScoredItem(item=one.item, confidence=score.confidence, why=score.why)
        for one, score in zip(line.candidates, scores, strict=True)
    ]
    if ranks:
        candidates.sort(key=lambda one: (one.confidence is None, -(one.confidence or 0)))
    return dataclasses.replace(line, candidates=candidates)


def _line(
    item: LineItem,
    *,
    query: str,
    item_code: str | None = None,
    product: CatalogItem | None = None,
    how: str,
    why: str,
    candidates: Sequence[Candidate] = (),
) -> MatchedLine:
    return MatchedLine(
        index=item.sr_no,
        verbatim=item.description or "",
        query=query,
        customer_code=item.customer_item_code,
        quantity=item.quantity,
        uom=item.uom,
        item_code=item_code,
        item=product,
        how=how,
        why=why,
        # Unscored here; `_scored` fills them in once the whole RFQ is scored.
        candidates=[ScoredItem(item=one.item) for one in candidates],
    )


def _unmatched(item: LineItem, why: str) -> MatchedLine:
    return MatchedLine(
        index=item.sr_no,
        verbatim=item.description or "",
        query="",
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
