"""A folder on disk, pretending to be blob storage.

Not a fallback and not a toy: this is what the tests run against, and what a
developer with no Azure account develops against. Same three methods, same
keys, same answers - so a bug that only appears against one of the two is a bug
in that one and not in the code above.

A key with slashes becomes nested directories, which also means the folder ends
up looking exactly like `Database/` did.
"""

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Everything a key may contain. A key is built from a record id and a filename
# that has already been through `_safe_name`, but this is the last place before
# a string becomes a path and it does not get to trust its caller.
UNSAFE = re.compile(r"[^\w./()\- ]+", re.UNICODE)


class FolderBlobs:
    """Bytes under a directory, keyed by relative path."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        del content_type  # a file on disk carries no type; the record holds it
        target = self._path(key)
        if target is None:
            logger.warning("Refusing to write a key that leaves the root: %r", key)
            return
        await asyncio.to_thread(_write, target, data)

    async def get(self, key: str) -> bytes | None:
        target = self._path(key)
        if target is None or not target.is_file():
            return None
        return await asyncio.to_thread(target.read_bytes)

    async def delete(self, key: str) -> None:
        target = self._path(key)
        if target is not None and target.is_file():
            target.unlink()

    def _path(self, key: str) -> Path | None:
        """Where this key lives, or None when it would land outside the root.

        `../../.env` is the case this exists for. A key reaches here from a
        record, and a record can be written from an email, so the last check
        before the filesystem is not the place to assume good intentions.
        """
        cleaned = key.replace("\\", "/").lstrip("/")
        parts = [UNSAFE.sub("_", part).strip(". ") for part in cleaned.split("/")]
        if not all(parts):
            return None

        target = (self._root / "/".join(parts)).resolve()
        root = self._root.resolve()
        if root != target and root not in target.parents:
            return None
        return target


def _write(target: Path, data: bytes) -> None:
    """Write whole or not at all.

    The same `os.replace` the record store uses: a page reading this folder
    while the pipeline writes into it sees the old file or the new one, never
    half of either.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
