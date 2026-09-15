"""How often the right product is in the shortlist at all.

    uv run python -m src.tools.catalog_recall

This is the one number that bounds everything built on the catalogue. Whatever
chooses between candidates later - a model, an operator - can only choose from
what it was shown, so a right answer that never reaches the shortlist is lost
before anybody looks at it. Tune the search against this, not against the
model's judgement.

The cases come out of the sheet itself: each row says what a customer once
asked for and which product the desk quoted, and the search is asked to find
the product from the wording.

What is indexed is our own description column, and the wording asked with is
the customer's - two different columns, so a case is never searching for a
sentence that is in the index. Where a variant does index the customer wording,
that case's own sentence is hidden first, because otherwise it would be
searching for itself, which scores perfectly and proves nothing. The product
itself always stays: removing it would ask whether we can find something that
is not there, which is not a question about the search.
"""

import logging
import sys
from collections import Counter
from dataclasses import dataclass

from src.core.config import Settings, get_settings
from src.core.logging import configure_logging
from src.domain.rules.catalog import Catalog, normalize_code
from src.infrastructure.catalog import Snapshot

logger = logging.getLogger(__name__)

AT = (1, 3, 5, 10, 20)
# What the desk typed where they could not read a value off their screenshots,
# and the buckets they use when no real product fits. Neither is a product, and
# "finding" one would be the wrong answer rather than a right one.
MISSING = (
    "not visible",
    "not provided",
    "not transcribed",
    "partially visible",
    "truncated",
    "…",
)
GENERIC = ("xx", "assorted items")
@dataclass(frozen=True, slots=True)
class Variant:
    """One way of searching the same rows.

    `indexed` puts the customer wording into the index beside our own, which
    also means that case's own sentence has to be hidden. `repair` is the
    misspelling fallback: `SUGER` shares no token with `SUGAR`, and without it
    a query of one misspelt word scores zero against everything.
    """

    description_column: str
    customer_description_column: str
    indexed: bool = False
    repair: bool = True


@dataclass(frozen=True, slots=True)
class Case:
    """One line as a customer wrote it, and the product the desk quoted."""

    wording: str
    expected_code: str


def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    rows = Snapshot(settings.CATALOG_SNAPSHOT_PATH).rows()
    if not rows:
        logger.error(
            "No catalogue at %s - run `python -m src.tools.sync_catalog` first",
            settings.CATALOG_SNAPSHOT_PATH,
        )
        return 1

    cases = cases_of(rows, settings)
    if not cases:
        logger.error("No usable cases: no row has both a customer wording and a product")
        return 1

    logger.info("%d row(s) in the sheet, %d usable case(s)", len(rows), len(cases))
    logger.info("Asked with the customer's wording, answered from our own descriptions.")
    logger.info("")

    scored = {
        name: _measure(name, rows, cases, variant, settings)
        for name, variant in _variants(settings).items()
    }
    _report_misses(rows, cases, scored[LIVE], settings)

    return 0


LIVE = "as it runs"


def _variants(settings: Settings) -> dict[str, Variant]:
    """The choices worth comparing. The catalogue is the same in all of them.

    The first is what production does. The other two take one thing away each,
    so that what it is worth is a number rather than an opinion.
    """
    ours = settings.CATALOG_ITEM_DESCRIPTION_COLUMN
    theirs = settings.CATALOG_CUSTOMER_DESCRIPTION_COLUMN
    return {
        LIVE: Variant(ours, theirs),
        "without typo repair": Variant(ours, theirs, repair=False),
        "customer wording too": Variant(ours, theirs, indexed=True),
    }


def _measure(
    name: str,
    rows: list[dict[str, str]],
    cases: list[Case],
    variant: Variant,
    settings: Settings,
) -> list[int | None]:
    """Recall for one way of indexing.

    A case's own wording is hidden only from a variant that indexes it: the
    others never had it in the index, so there is nothing to hide.
    """
    ranks = [
        _rank_of(
            _build(_without(rows, case, settings) if variant.indexed else rows, variant, settings),
            case,
            max(AT),
            case.wording,
        )
        for case in cases
    ]

    size = len(_build(rows, variant, settings))
    scores = "  ".join(
        f"@{at}: {100 * sum(1 for r in ranks if r is not None and r <= at) / len(ranks):5.1f}%"
        for at in AT
    )
    logger.info("%-22s %3d items   %s", name, size, scores)
    return ranks


def _report_misses(
    rows: list[dict[str, str]],
    cases: list[Case],
    ranks: list[int | None],
    settings: Settings,
) -> None:
    """The ones nothing found. This list is the work queue for the search."""
    missed = [case for case, rank in zip(cases, ranks, strict=True) if rank is None]
    whole = _build(rows, _variants(settings)[LIVE], settings)

    logger.info("")
    logger.info("Never found, %d case(s):", len(missed))
    for case in missed[:12]:
        found = whole.by_code(case.expected_code)
        logger.info("  asked for: %s", case.wording[:66])
        logger.info("  we sell:   %-12s %s", case.expected_code, (found.description if found else "")[:52])

    # A product the sheet mentions once has, with that one row removed, only
    # our own shelf description left. A miss there says the two wordings share
    # no words at all - a fact about the catalogue, not about the ranking.
    counts = Counter(normalize_code(row.get(settings.CATALOG_CODE_COLUMN, "")) for row in rows)
    alone = sum(1 for case in missed if counts[normalize_code(case.expected_code)] == 1)

    logger.info("")
    logger.info("  %d of them the sheet mentions only once", alone)
    logger.info("  %d of them it mentions more than once", len(missed) - alone)


def _build(rows: list[dict[str, str]], variant: Variant, settings: Settings) -> Catalog:
    return Catalog.from_rows(
        rows,
        code_column=settings.CATALOG_CODE_COLUMN,
        description_column=variant.description_column,
        customer_description_column=variant.customer_description_column,
        customer_code_column=settings.CATALOG_CUSTOMER_CODE_COLUMN,
        index_customer_description=variant.indexed,
        repair_misspellings=variant.repair,
    )


def _without(
    rows: list[dict[str, str]], case: Case, settings: Settings
) -> list[dict[str, str]]:
    """Every row, with this one case's customer wording blanked out.

    The row itself stays. Dropping it would take the product out of the
    catalogue as well, and the measurement would then be asking whether we can
    find something that is not there - which is not a question about the
    search. What has to go is only the sentence being searched for.

    This is also what happens for real: the product is in the sheet, its past
    wording is in the sheet, and a *new* wording arrives that is not.
    """
    wording = settings.CATALOG_CUSTOMER_DESCRIPTION_COLUMN
    code = settings.CATALOG_CODE_COLUMN

    hidden = []
    for row in rows:
        if row.get(wording, "").strip() == case.wording and normalize_code(
            row.get(code, "")
        ) == normalize_code(case.expected_code):
            row = {**row, wording: ""}
        hidden.append(row)
    return hidden


def _rank_of(catalog: Catalog, case: Case, limit: int, query: str) -> int | None:
    """Where the right product came in the search, or None for "nowhere"."""
    codes = [normalize_code(one.item.code) for one in catalog.search(query, limit)]
    expected = normalize_code(case.expected_code)
    return codes.index(expected) + 1 if expected in codes else None


def cases_of(rows: list[dict[str, str]], settings: Settings) -> list[Case]:
    """Rows that say both what was asked for and what was sold."""
    cases = []
    for row in rows:
        wording = _real(row.get(settings.CATALOG_CUSTOMER_DESCRIPTION_COLUMN, ""))
        code = _real(row.get(settings.CATALOG_CODE_COLUMN, ""))
        description = _real(row.get(settings.CATALOG_ITEM_DESCRIPTION_COLUMN, ""))
        if wording and code and description and not _is_generic(code, description):
            cases.append(Case(wording=wording, expected_code=code))
    return cases


def _real(value: str) -> str:
    lowered = value.strip().lower()
    return "" if not lowered or any(word in lowered for word in MISSING) else value.strip()


def _is_generic(code: str, description: str) -> bool:
    lowered = f"{code} {description}".lower()
    return any(word in lowered for word in GENERIC)


if __name__ == "__main__":
    sys.exit(main())
