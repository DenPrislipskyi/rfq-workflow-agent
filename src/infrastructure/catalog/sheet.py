"""The product list as Google serves it: one CSV over one GET.

No API client and no credentials, because the sheet is published - "anyone with
the link can view" - and the export endpoint then answers a plain HTTP request.
That is a decision about the sheet rather than about this code: a private sheet
needs a service account, and this module would grow an auth header.

CSV rather than xlsx: the sheet is a flat table, and a text format is one a
person can open, diff and read in the snapshot afterwards.
"""

import logging
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/export"

# Google answers an unshared sheet with a sign-in page, not with an error. The
# status says 401, and the body is HTML either way - so both are checked.
HTML = "text/html"
NOT_SHARED = (
    "Google will not export sheet {sheet_id}: it is not published. Open it, "
    "Share -> General access -> Anyone with the link -> Viewer."
)
NOT_CSV = (
    "Google answered with a web page instead of CSV for sheet {sheet_id}. "
    "Either it is not published, or gid={gid} is not a tab in it."
)


class SheetUnavailable(Exception):
    """The sheet could not be read. The message says what to do about it."""


class Sheet(Protocol):
    """What the catalogue service needs of a source: CSV text, or an exception.

    A protocol so the service depends on "something that hands over the list"
    rather than on Google: a file, a private sheet behind a service account or
    a fake in a test all satisfy it without the service knowing.
    """

    async def fetch(self) -> str: ...


class PublishedSheet:
    """One tab of one published Google Sheet, fetched as CSV."""

    def __init__(self, client: httpx.AsyncClient, sheet_id: str, gid: str) -> None:
        self._client = client
        self._sheet_id = sheet_id
        self._gid = gid

    async def fetch(self) -> str:
        """The tab as CSV text. Raises `SheetUnavailable` with the reason."""
        url = EXPORT_URL.format(sheet_id=self._sheet_id)
        params = {"format": "csv", "gid": self._gid}

        try:
            response = await self._client.get(url, params=params, follow_redirects=True)
        except httpx.HTTPError as error:
            raise SheetUnavailable(f"Could not reach Google: {error}") from error

        if response.status_code in (401, 403):
            raise SheetUnavailable(NOT_SHARED.format(sheet_id=self._sheet_id))
        if response.status_code >= 400:
            raise SheetUnavailable(
                f"Google answered {response.status_code} for sheet {self._sheet_id}"
            )
        if HTML in response.headers.get("content-type", ""):
            raise SheetUnavailable(NOT_CSV.format(sheet_id=self._sheet_id, gid=self._gid))

        logger.info("Fetched %d KB of CSV from sheet %s", len(response.content) // 1024, self._sheet_id)
        return response.text
