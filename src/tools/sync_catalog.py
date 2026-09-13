"""Copy the desk's product sheet into the snapshot the agent reads.

    uv run python -m src.tools.sync_catalog

The running service does this by itself every `CATALOG_REFRESH_MINUTES`. This
is the same call by hand, for the two moments it is wanted: the first one,
before there is any snapshot at all, and the one right after somebody adds a
product and does not want to wait.

Prints what it got, and what the catalogue can be searched by. Needs no model
and no mailbox - only that the sheet is published.
"""

import asyncio
import logging
import sys
from collections import Counter

import httpx

from src.core.config import get_settings
from src.core.lifespan import build_catalog
from src.core.logging import configure_logging
from src.domain.rules.catalog import Catalog, normalize_code
from src.infrastructure.catalog import SheetUnavailable

logger = logging.getLogger(__name__)


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    if not settings.CATALOG_SHEET_ID:
        logger.error("CATALOG_SHEET_ID is empty - there is no sheet to copy from")
        return 1

    async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
        service = build_catalog(settings, client)

        try:
            catalog = await service.refresh()
        except SheetUnavailable as error:
            # The reason is the whole message, and it names what to do about it.
            logger.error("%s", error)
            return 1

    logger.info("Snapshot is at %s", settings.CATALOG_SNAPSHOT_PATH)
    _report_duplicates(catalog)
    return 0


def _report_duplicates(catalog: Catalog) -> None:
    """Say when one code names many rows.

    Not an error and not fixed here - it is the sheet's to fix - but it is
    worth seeing: a code that repeats is a code that resolves to whichever row
    was last, and a description that repeats forty times is a placeholder
    somebody left in rather than a product.
    """
    codes = Counter(normalize_code(item.code) for item in catalog.items)
    repeated = [(code, count) for code, count in codes.most_common(5) if count > 1]

    # The customer's own code is what a line of an RFQ quotes, and matching is
    # written for one code naming one product. A code on two products resolves
    # to whichever row came first, so the sheet is where it gets fixed.
    theirs = Counter(
        normalize_code(item.customer_code) for item in catalog.items if item.customer_code
    )
    for code, count in theirs.most_common():
        if count < 2:
            break
        items = sorted({one.code for one in catalog.items
                        if normalize_code(one.customer_code) == code})
        if len(items) > 1:
            logger.warning(
                "Customer code %s names %d different products: %s",
                code, len(items), ", ".join(items),
            )

    logger.info("%d row(s), %d distinct code(s)", len(catalog), len(codes))
    if not repeated:
        return

    logger.warning("Some codes name more than one row - a lookup finds the last of them:")
    for code, count in repeated:
        logger.warning("  %-24s %d rows", code, count)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
