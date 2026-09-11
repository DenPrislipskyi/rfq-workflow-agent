"""A "something changed" signal, for pages watching a list.

Deliberately thin: it carries no payload and names nothing that changed. The
only answer a watching page has to that question is "read the list again", so
saying more would be inventing a second contract nobody reads.

In-process, and that is the whole scope: the webhook writes and the page reads
inside one service. A second process writing records - the backfill tool - is
not heard by a running server, which is why that tool is documented as
something you run into an empty folder rather than beside a live one.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager


class Changes:
    """Listeners wait on it; whoever writes announces to all of them."""

    def __init__(self) -> None:
        self._listeners: set[asyncio.Event] = set()

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Event]:
        """One listener, dropped again when the caller is done with it.

        A context manager because the caller is a streaming response: a page
        closed mid-stream must not leave its event behind to be set forever.
        """
        listener = asyncio.Event()
        self._listeners.add(listener)
        try:
            yield listener
        finally:
            self._listeners.discard(listener)

    def announce(self) -> None:
        """Tell every listener that the list is not what it was.

        Coalescing, because the listeners are events rather than queues: a page
        that was busy while three emails arrived is woken once, and one re-read
        answers all three. A backlog here could never be spent.
        """
        for listener in self._listeners:
            listener.set()

    @property
    def listeners(self) -> int:
        """How many pages are watching. For logging, and for tests."""
        return len(self._listeners)
