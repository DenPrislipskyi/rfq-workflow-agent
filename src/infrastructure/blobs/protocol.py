"""Somewhere to put bytes, and get them back by key.

Three methods, because that is all the pipeline ever does with a file: keep it,
fetch it, and tell whether it is there. No listing, no moving, no signed URLs -
the record is what says which keys exist, and the browser gets files through
the API rather than from storage directly.

A protocol rather than a base class so that the folder implementation beside it
owes nothing to the Azure one, and so that 885 tests keep running with no
network and no account.
"""

from typing import Protocol


class Blobs(Protocol):
    """Bytes by key. Keys look like `<record id>/attachments/<filename>`."""

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        """Write, overwriting whatever was there.

        Overwrite rather than fail: a record is rewritten as the pipeline moves
        through it, and the second write of the same form is the normal case.
        """
        ...

    async def get(self, key: str) -> bytes | None:
        """Read, or None when there is nothing under that key.

        None rather than an exception: a key that is not there is an ordinary
        answer to give a browser, and a 404 is the right shape for it.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove. Silent when the key was not there to begin with."""
        ...
