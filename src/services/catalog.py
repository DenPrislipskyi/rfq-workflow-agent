"""Keeping the in-memory product list current.

One job, and the order of it is the whole design:

    snapshot on disk  ->  indexed at boot, before anything is served
    Google Sheet      ->  fetched afterwards, and every REFRESH_MINUTES

So the agent starts with a catalogue even when Google is down, and an email
that arrives mid-refresh is matched against the last good copy rather than
against nothing. A failed fetch is logged and retried; it never replaces a
catalogue that works with one that does not.

The sheet stays the place people edit. An item typed in by hand is in the
system on the next refresh, which is the whole reason the source is a sheet and
not a file in the repository.
"""

import asyncio
import logging
from contextlib import suppress

from src.domain.rules.catalog import Catalog
from src.infrastructure.catalog import Sheet, SheetUnavailable, Snapshot, parse_csv

logger = logging.getLogger(__name__)

RETRY_MINUTES = 2
# Google is not part of the first email of the day: the boot fetch runs behind
# the server coming up, exactly as the Graph subscription does.
STARTUP_DELAY_SECONDS = 2.0


class CatalogService:
    """The catalogue the rest of the service reads, and the loop that renews it."""

    def __init__(
        self,
        sheet: Sheet | None,
        snapshot: Snapshot,
        *,
        code_column: str,
        description_column: str,
        refresh_minutes: int,
        customer_code_column: str = "",
        customer_description_column: str = "",
        index_item_description: bool = False,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
    ) -> None:
        # None when no sheet is configured: the snapshot on disk is then the
        # whole story, which is how the tools and the tests run.
        self._sheet = sheet
        self._snapshot = snapshot
        self._code_column = code_column
        self._description_column = description_column
        self._customer_code_column = customer_code_column
        self._customer_description_column = customer_description_column
        self._index_item_description = index_item_description
        self._refresh_minutes = refresh_minutes
        self._startup_delay_seconds = startup_delay_seconds

        self._catalog = Catalog([])
        self._task: asyncio.Task | None = None

    @property
    def current(self) -> Catalog:
        """The catalogue as it stands. Empty rather than missing, so that a
        caller never has to ask whether the sheet has been read yet."""
        return self._catalog

    def load(self) -> Catalog:
        """Index whatever is on disk. Never raises: no snapshot is an empty
        catalogue, and an empty catalogue matches nothing rather than wrongly."""
        self._catalog = self._index(self._snapshot.rows())
        info = self._snapshot.info()

        if len(self._catalog):
            logger.info(
                "Catalogue: %d item(s) from %s, taken %s ago",
                len(self._catalog),
                self._snapshot.path,
                info.age if info else "an unknown time",
            )
        else:
            logger.warning(
                "Catalogue is empty - no snapshot at %s. Run "
                "`python -m src.tools.sync_catalog`",
                self._snapshot.path,
            )
        return self._catalog

    async def refresh(self) -> Catalog:
        """Fetch the sheet, keep it, index it. Raises `SheetUnavailable`.

        The new copy is only put in front of the old one once it has parsed
        into something: a sheet that comes back empty is a sheet somebody is
        editing, not a catalogue.
        """
        if self._sheet is None:
            raise SheetUnavailable("No sheet is configured - CATALOG_SHEET_ID is empty")

        text = await self._sheet.fetch()
        # Off the event loop: tens of thousands of rows are parsed and indexed
        # here, and the webhook must stay answerable while that happens.
        catalog = await asyncio.to_thread(self._index, parse_csv(text))

        if not len(catalog):
            raise SheetUnavailable(
                "The sheet has no rows with both "
                f"{self._code_column!r} and {self._description_column!r} in them"
            )

        info = self._snapshot.write(text)
        self._catalog = catalog
        logger.info("Catalogue refreshed: %d item(s), sha %s", len(catalog), info.sha256[:8])
        return catalog

    async def start(self) -> None:
        """Return at once; the sheet is fetched behind the running server."""
        if self._sheet is None:
            logger.info("CATALOG_SHEET_ID is empty - the snapshot on disk is all there is")
            return
        self._task = asyncio.create_task(self._run(), name="catalog-refresh")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def status(self) -> dict:
        """What the catalogue is, for a health check and for the logs."""
        info = self._snapshot.info()
        return {
            "items": len(self._catalog),
            "fetched_at": info.fetched_at if info else None,
            "age": info.age if info else None,
        }

    async def _run(self) -> None:
        await asyncio.sleep(self._startup_delay_seconds)

        while True:
            try:
                await self.refresh()
                delay = self._refresh_minutes * 60
            except SheetUnavailable as error:
                # The message names what to do, so it goes in whole and without
                # a traceback: nothing here failed, the sheet is not readable.
                logger.warning("Catalogue not refreshed: %s", error)
                delay = RETRY_MINUTES * 60
            except Exception:
                logger.exception("Catalogue refresh failed")
                delay = RETRY_MINUTES * 60

            await asyncio.sleep(delay)

    def _index(self, rows: list[dict[str, str]]) -> Catalog:
        return Catalog.from_rows(
            rows,
            code_column=self._code_column,
            description_column=self._description_column,
            customer_code_column=self._customer_code_column,
            customer_description_column=self._customer_description_column,
            index_item_description=self._index_item_description,
        )
