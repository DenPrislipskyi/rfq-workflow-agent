"""What the pipeline needs from a record store, and nothing more.

Five methods. Two write, three read, and both implementations answer them the
same way - one against a folder on disk, one against Postgres and a blob
container. The pipeline cannot tell which it has, which is the point: the
tests run against the folder with no network, and the service runs against the
database with the same code above it.

`EmailRecord` crosses this boundary in both directions. It is a Pydantic model
and it belongs to neither store: the folder one writes it as JSON, the database
one takes it apart into rows and puts it back together on the way out. What the
API renders is the same object either way.
"""

from collections.abc import Sequence
from typing import Protocol

from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.infrastructure.documents import SourceFile
from src.infrastructure.storage.records import (
    EmailRecord,
    RecordedDelivery,
    RecordedExtraction,
    RecordedMatch,
)


class Records(Protocol):
    """One record per email: opened at triage, filled in as the pipeline runs."""

    async def open(
        self,
        *,
        email: NormalizedEmail,
        outcome: ClassificationOutcome | None,
        decision_id: str | None,
        source: str,
        files_note: str = ...,
    ) -> str | None:
        """Start a record the moment there is a verdict. Returns its id.

        None means nothing was recorded - the store is switched off, or writing
        failed. Either way the caller carries on: a record that cannot be
        written must not cost the email.
        """
        ...

    async def update(
        self,
        record_id: str | None,
        *,
        files: Sequence[SourceFile] | None = None,
        labels: Sequence[str] | None = None,
        labelled: bool | None = None,
        extraction: RecordedExtraction | None = None,
        matching: Sequence[RecordedMatch] | None = None,
        delivery: RecordedDelivery | None = None,
        form: tuple[str, bytes] | None = None,
        error: str | None = None,
    ) -> None:
        """Add what became of the email. Every argument is optional.

        Overwrites rather than appends: this is the current state of one email,
        and the account of how it got there is the journal's job.
        """
        ...

    async def all(self) -> list[EmailRecord]:
        """Every record, newest first."""
        ...

    async def read(self, record_id: str) -> EmailRecord | None:
        """One record by id, or None when there is no such record."""
        ...

    async def file(self, record_id: str, saved_as: str) -> bytes | None:
        """One file out of a record, by the name the record gave it.

        `saved_as` arrives from a URL. Neither implementation joins it onto a
        path: the folder one resolves it and refuses anything that lands
        outside the record, the database one looks it up as a value. A guessed
        name is a 404 rather than a file.
        """
        ...
