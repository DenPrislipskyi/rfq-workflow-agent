"""The lines of an RFQ against our own product list.

    judge the rows a code landed on    1 model call per 50 rows
    look each line up                  no model

The desk's specification, in three branches:

    the customer's code names a row of the sheet, and that row's own two
    descriptions are the same product   -> that row, whole, and nothing else
    the code names a row whose two descriptions are different products
                                        -> the row is a bad mapping: drop its
                                           item code, search the sheet with the
                                           row's customer wording, top five
    the code names nothing, or there was no code
                                        -> search the sheet with the line's own
                                           item description, top five

What is checked in the first two branches is the *sheet*, not the email. A row
is one past mapping, and some of them are wrong: row 1 of today's snapshot has
a customer asking for `EXTERNAL HDD 4TB` against an item that is a fishing rod.
Every search, in both branches that have one, reads the
`Item Description / SSG Description` column.

Only the last two produce a shortlist. A confirmed code produces one product,
because there was nothing to choose between.
"""

import logging
import re
from collections.abc import Sequence

from src.domain.rules.catalog import Candidate, Catalog, CatalogItem, tokenize
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

logger = logging.getLogger(__name__)

NO_CATALOGUE = "There is no catalogue to match against."
NOTHING_FOUND = "The sheet had nothing to offer for this line."
CONFIRMED = "The customer's code names this row, and its two descriptions are the same product."
REJECTED = (
    "The customer's code names {code}, whose two descriptions are different products - "
    "the row is a bad mapping, so the sheet was searched by its customer wording instead."
)
SEARCHED = "No row carries this customer code, so the sheet was searched by the requested description."
NO_CODE = "The customer quoted no code, so the sheet was searched by the requested description."

# One row of the sheet, as the judge is asked about it.
Pair = tuple[str, str]

# "65MM" is the number 65 with a unit stuck to it, and the sheet writes the
# same size both ways. Only the scoring widens like this; the index does not.
NUMBER_AND_UNIT = re.compile(r"^(\d+(?:[.,]\d+)?)([a-z]{1,4})$")


class MatchingPipeline:
    """Turns the lines of one RFQ into products, or into reasons there are none."""

    def __init__(self, judge: AgreementJudge, *, candidates: int = 5) -> None:
        self._judge = judge
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
        verdicts = await self._judged(rows)

        matched = [
            self._one(item, row, verdicts.get(_pair(row)), catalog)
            for item, row in zip(items, rows, strict=True)
        ]
        _log(matched)
        return matched

    async def _judged(
        self, rows: Sequence[CatalogItem | None]
    ) -> dict[Pair, tuple[bool, str]]:
        """Every distinct row a code landed on, judged once.

        Distinct by its two descriptions rather than by its item code: two
        lines of one RFQ quoting the same code ask the same question, and the
        answer does not depend on which line asked.
        """
        pairs = list(dict.fromkeys(_pair(row) for row in rows if row is not None))
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
            return _line(
                item,
                query="",
                item_code=row.code,
                product=row,
                how=CODE_CONFIRMED,
                why=_and(CONFIRMED, why),
            )

        return self._searched(
            item,
            row.customer_description,
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


def _pair(row: CatalogItem | None) -> Pair:
    """The two sentences of a row, as the judge is asked about them."""
    return ("", "") if row is None else (row.customer_description, row.description)


def _and(reason: str, said: str) -> str:
    """The branch's own reason, and what the model said about the row."""
    return f"{reason} {said}".strip() if said else reason


def _scored(candidates: Sequence[Candidate], query: str) -> list[ScoredItem]:
    """How much of what we searched for each candidate actually carries.

    A percentage of the **query's** words, not of the best candidate's score.
    The difference is the whole point: a score relative to the best one makes
    the best one 100% by construction - it is divided by itself - so the top
    row of every shortlist claimed certainty it never had, whether it was the
    right bolt or a box of eggs.

    Absolute, so it means the same thing on every line and can be read without
    the rest of the list. Measured on the sheet: a query whose product is not
    in the catalogue at all scores its top candidate 11-17%, and one whose
    product is there scores it 43-60%. That gap is what the number is for.

    Ranking stays BM25's job. It knows which words are rare, and rarity is
    what tells two bolts apart; counting words does not. This only says how
    much of the question each answer covers.
    """
    asked = _words(query)
    return [
        ScoredItem(
            item=candidate.item,
            confidence=_covered(asked, _words(candidate.item.description)),
        )
        for candidate in candidates
    ]


def _words(text: str) -> set[str]:
    """The words of one sentence, as this scoring counts them.

    The search's own tokenizer, and then one thing more: a number welded to a
    unit also counts as the bare number, so `65MM` and `65` are not two
    different sizes. Nothing here reaches the index.
    """
    words: set[str] = set()
    for word in tokenize(text):
        words.add(word)
        if match := NUMBER_AND_UNIT.match(word):
            words.add(match.group(1))
    return words


def _covered(asked: set[str], offered: set[str]) -> int:
    """A percentage of the words we asked with, not of the words it has.

    An item described at length is not punished for saying more than it was
    asked; a line whose every word is there is a full answer however much the
    sheet goes on about it.
    """
    return round(100 * len(asked & offered) / len(asked)) if asked else 0


def _line(
    item: LineItem,
    *,
    query: str,
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
        query=query,
        customer_code=item.customer_item_code,
        quantity=item.quantity,
        uom=item.uom,
        item_code=item_code,
        item=product,
        how=how,
        why=why,
        confidence=confidence,
        candidates=_scored(candidates, query),
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
