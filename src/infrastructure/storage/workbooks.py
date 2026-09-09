"""Where the filled RFQ forms are kept.

Every copy the agent produces is written here under the id of the decision that
produced it, so a line in the journal and the file it describes can be put side
by side. Until the forwarding step exists this folder is the only way to see
what came out; afterwards it stays as the record of what was actually sent.
"""

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# The name is an id we generated, but it reaches the filesystem, so it is
# checked rather than trusted.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkbookStore:
    """A folder of forms, one per RFQ."""

    def __init__(self, directory: Path, *, enabled: bool, suffix: str = ".xlsx") -> None:
        self._directory = directory
        self._enabled = enabled
        self._suffix = suffix

        if enabled:
            directory.mkdir(parents=True, exist_ok=True)
            logger.info("Filled workbooks are kept in %s", directory)

    async def save(self, name: str, data: bytes) -> Path | None:
        """Write one workbook, or return None when the store is switched off.

        Never raises: a copy that could not be kept is worth a warning, and
        nothing more - the file itself is already in hand.
        """
        if not self._enabled:
            return None

        path = self._directory / f"{_UNSAFE.sub('-', name) or 'workbook'}{self._suffix}"
        try:
            await asyncio.to_thread(path.write_bytes, data)
        except OSError as error:
            logger.warning("Could not write %s: %s", path, error)
            return None
        return path
