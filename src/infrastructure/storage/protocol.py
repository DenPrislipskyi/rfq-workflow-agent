"""What the pipeline needs from a record store, and nothing more.

Seven methods. Four write, three read, and both implementations answer them the
same way - one against a folder on disk, one against Postgres and a blob
container. The pipeline cannot tell which it has, which is the point: the
tests run against the folder with no network, and the service runs against the
database with the same code above it.

`EmailRecord` crosses this boundary in both directions. It is a Pydantic model
and it belongs to neither store: the folder one writes it as JSON, the database
one takes it apart into rows and puts it back together on the way out. What the
API renders is the same object either way.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol

from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.infrastructure.documents import SourceFile
from src.infrastructure.storage.records import (
    EmailRecord,
    RecordedApproval,
    RecordedDelivery,
    RecordedExtraction,
    RecordedInquiry,
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

    async def confirm(self, record_id: str, index: int, item_code: str | None) -> bool:
        """Settle one line on a product, or unsettle it. True when it took.

        False means there is no such record or no such line in it. Whether the
        product exists is not asked here and cannot be: a store holds records,
        not the sheet, and the shortlist is not the only place a product may
        come from - a person who finds none of the five right goes and picks
        the sixth by hand. The endpoint checks the code against the catalogue
        before it gets this far.

        `None` clears it, which is not a third state: a line nobody has settled
        and a line somebody changed their mind about read the same, because
        they are the same - there is nothing confirmed either way.
        """
        ...

    async def price(self, record_id: str, index: int, unit_price: float | None) -> bool:
        """Record what a supplier quoted for one unit of a line. True when it took.

        False means there is no such record or no such line in it. Whether the
        price is a sensible one is not asked here: a store holds what it was
        told, and the endpoint has already refused anything that is not a
        positive number.

        `None` clears it, for the same reason `confirm` takes one - an offer
        withdrawn and an offer never made are the same state.
        """
        ...

    async def inquire(self, record_id: str, inquiries: Sequence[RecordedInquiry]) -> bool:
        """Record the letters that went to the suppliers. True when it took.

        False for a record that does not exist, and false for one that has
        already been asked: the letters go out once. A second send is refused
        here rather than merged, because "what we sent" stops being an answer
        the moment it can be rewritten after the fact.

        The whole batch at once, not one supplier at a time. One click sends
        every letter, and half of them on the record would describe a send
        that never happened.
        """
        ...

    async def approve(
        self,
        record_id: str,
        *,
        approval: RecordedApproval,
        prices: Mapping[int, float],
    ) -> bool:
        """Freeze what this RFQ sells for. True when it took.

        False for a record that does not exist, and false for one already
        approved: the sign-off happens once. There is no way back, and that is
        deliberate - a quotation that can be un-approved is a quotation nobody
        downstream can rely on.

        Whether every line is covered is not asked here: a store holds what it
        was told, and the endpoint has already refused an approval with holes
        in it. What the store will not do is write the moment without the
        prices it approved.
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
