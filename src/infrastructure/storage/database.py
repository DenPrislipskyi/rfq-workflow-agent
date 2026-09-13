"""The record store, in Postgres, with the bytes in blob storage.

The other half of `records.py`. Same five methods, same `EmailRecord` going in
and coming out - what changes is where it lands: rows instead of a JSON file,
and a container instead of the folder beside it.

The split is the one from `docs/agent/DATABASE.md`:

    Postgres    the record - verdict, extraction, matching, delivery, labels
    blobs       the bytes  - the body, the attachments, the filled form

Nothing here writes a file that anything else has to find by path. A row holds
`saved_as`, which is what a URL carries, and `blob_key`, which is where the
bytes are. The front end sees only the first, and that is why moving off disk
changed no URL.

Two things are deliberate and load-bearing:

**A record that cannot be written must not cost the email.** Every method
swallows its own failure and says so in the log. The desk gets its RFQ whether
or not a row was written; the journal and the log still have the decision.

**The bytes go first, the row second.** A blob nobody references is litter. A
row pointing at a blob that is not there is a broken page.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.infrastructure.blobs import Blobs
from src.infrastructure.db import (
    Email,
    EmailDelivery,
    EmailExtraction,
    EmailFile,
    EmailVerdict,
    FileRole,
    RfqLine,
    RfqLineCandidate,
)
from src.infrastructure.documents import SourceFile
from src.infrastructure.storage.changes import Changes
from src.infrastructure.storage.records import (
    A_LINK,
    BODY,
    DOWNLOAD_FAILED,
    NOT_DOWNLOADED,
    NOT_KEPT,
    EmailRecord,
    RecordedAddress,
    RecordedCandidate,
    RecordedDelivery,
    RecordedExtraction,
    RecordedFile,
    RecordedMatch,
    RecordedVerdict,
    _address,
    _record_id,
    _safe_name,
    _unique,
    _verdict,
)

logger = logging.getLogger(__name__)

# The three header fields the screens read. Kept as columns beside the rest of
# the header so that "every RFQ for MV ALMI GLOBE" is a query rather than a
# scan through JSON.
VESSEL, IMO, PORT = "vessel_name", "imo", "delivery_port"

# Everything hanging off an email, loaded in one round trip. `selectinload`
# rather than a join: seven tables joined at once multiplies rows by every
# collection, and the lines alone can be sixty.
_WHOLE = (
    selectinload(Email.verdict),
    selectinload(Email.extraction),
    selectinload(Email.delivery),
    selectinload(Email.files),
    selectinload(Email.lines).selectinload(RfqLine.candidates),
)


class DatabaseRecords:
    """Records in Postgres, files in a container."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        blobs: Blobs,
        *,
        enabled: bool = True,
        keep_attachments: bool = True,
        changes: Changes | None = None,
    ) -> None:
        self._sessions = sessions
        self._blobs = blobs
        self._enabled = enabled
        self._keep = keep_attachments
        self._changes = changes or Changes()

    # --- writing ----------------------------------------------------------

    async def open(
        self,
        *,
        email: NormalizedEmail,
        outcome: ClassificationOutcome | None,
        decision_id: str | None,
        source: str,
        files_note: str = NOT_DOWNLOADED,
    ) -> str | None:
        if not self._enabled:
            return None

        now = datetime.now(UTC)
        record_id = _record_id(email.received_at or now, decision_id)

        try:
            # The body first: a row that says `body_key` and no blob behind it
            # is worse than a row that says nothing.
            body_key = None
            if email.body_text:
                body_key = f"{record_id}/{BODY}"
                await self._blobs.put(
                    body_key, email.body_text.encode("utf-8"), content_type="text/plain"
                )

            async with self._sessions() as session, session.begin():
                session.add(
                    Email(
                        id=record_id,
                        decision_id=decision_id,
                        message_id=email.message_id,
                        source=source,
                        received_at=email.received_at,
                        mailbox=email.mailbox,
                        sender_name=_name_of(email.sender),
                        sender_address=_address_of(email.sender),
                        recipients=[_wire(item) for item in email.to if item],
                        cc=[_wire(item) for item in email.cc if item],
                        subject=email.subject,
                        body_chars=len(email.body_text),
                        body_key=body_key,
                        verdict=_verdict_row(outcome),
                        # Names and sizes only. The bytes arrive with `update`,
                        # if the pipeline downloaded them at all.
                        files=[
                            EmailFile(
                                role=FileRole.ATTACHMENT,
                                filename=item.filename,
                                content_type=item.content_type,
                                size_bytes=item.size_bytes or 0,
                                note=files_note,
                            )
                            for item in email.attachments
                        ],
                    )
                )
        except Exception:
            logger.exception("Could not record %s", record_id)
            return None

        self._changes.announce()
        return record_id

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
        if not self._enabled or record_id is None:
            return

        try:
            async with self._sessions() as session, session.begin():
                row = await session.get(Email, record_id, options=_WHOLE)
                if row is None:
                    logger.warning("No record %s to update", record_id)
                    return

                if files is not None:
                    await self._replace_files(session, row, files)
                if labels is not None:
                    row.labels = list(labels)
                if labelled is not None:
                    row.labelled = labelled
                if extraction is not None:
                    row.extraction = _extraction_row(extraction)
                if matching is not None:
                    await _replace_lines(session, row, matching)
                if delivery is not None:
                    row.delivery = _delivery_row(delivery)
                if form is not None:
                    await self._replace_form(row, *form)
                if error is not None:
                    row.error = error
        except Exception:
            logger.exception("Could not update record %s", record_id)
            return

        self._changes.announce()

    async def _replace_files(
        self, session: AsyncSession, row: Email, files: Sequence[SourceFile]
    ) -> None:
        """The attachments, with their bytes where the pipeline had them.

        Replaces rather than merges: `update` is called once with the whole
        list, and a second call means the list itself changed.
        """
        form = [item for item in row.files if item.role is FileRole.FORM]
        taken = {name.lower() for name in (BODY, "email.json")}
        kept: list[EmailFile] = []

        for item in files:
            entry = EmailFile(
                role=FileRole.ATTACHMENT,
                filename=item.filename,
                content_type=item.content_type,
                size_bytes=item.size_bytes or (len(item.data) if item.data else 0),
            )
            if item.is_reference:
                entry.note = A_LINK
            elif item.data is None:
                entry.note = DOWNLOAD_FAILED
            elif not self._keep:
                entry.note = NOT_KEPT
            else:
                name = _unique(_safe_name(item.filename), taken)
                entry.saved_as = name
                entry.blob_key = f"{row.id}/attachments/{name}"
                await self._blobs.put(entry.blob_key, item.data, content_type=item.content_type)
            kept.append(entry)

        row.files = kept + form
        await session.flush()

    async def _replace_form(self, row: Email, filename: str, data: bytes) -> None:
        """The filled form, beside the email it was made from.

        Its name must not collide with a customer's: however unlikely, our file
        overwriting theirs is the one way this store could lose an attachment.
        """
        taken = {name.lower() for name in (BODY, "email.json")} | {
            item.saved_as.lower() for item in row.files if item.saved_as
        }
        name = _unique(_safe_name(filename), taken)
        key = f"{row.id}/form/{name}"
        await self._blobs.put(key, data)

        row.files = [item for item in row.files if item.role is not FileRole.FORM] + [
            EmailFile(
                role=FileRole.FORM,
                filename=filename,
                size_bytes=len(data),
                saved_as=name,
                blob_key=key,
            )
        ]

    async def aclose(self) -> None:
        """Let go of the blob client. Called from the lifespan's shutdown.

        Only the Azure one holds anything - a folder needs no closing - so this
        asks rather than assumes.
        """
        closing = getattr(self._blobs, "close", None)
        if closing is not None:
            await closing()

    # --- reading ----------------------------------------------------------

    async def all(self) -> list[EmailRecord]:
        if not self._enabled:
            return []
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Email).options(*_WHOLE).order_by(Email.created_at.desc())
            )
            return [_record(row) for row in rows]

    async def read(self, record_id: str) -> EmailRecord | None:
        if not self._enabled:
            return None
        async with self._sessions() as session:
            row = await session.get(Email, record_id, options=_WHOLE)
            return _record(row) if row else None

    async def file(self, record_id: str, saved_as: str) -> bytes | None:
        """Looked up as a value, never joined onto anything.

        A name that is not in this record's rows does not resolve, so a guessed
        one cannot reach another record's bytes however it is spelled.
        """
        if not self._enabled:
            return None
        async with self._sessions() as session:
            key = await session.scalar(
                select(EmailFile.blob_key).where(
                    EmailFile.email_id == record_id, EmailFile.saved_as == saved_as
                )
            )
        return await self._blobs.get(key) if key else None


# --- rows out of the pipeline's models ------------------------------------


def _verdict_row(outcome: ClassificationOutcome | None) -> EmailVerdict | None:
    verdict = _verdict(outcome)
    if verdict is None:
        return None
    return EmailVerdict(**verdict.model_dump())


def _extraction_row(extraction: RecordedExtraction) -> EmailExtraction:
    """The header's three named fields lifted out, the rest kept whole."""
    header = extraction.header or {}
    return EmailExtraction(
        items=extraction.items,
        complete=extraction.complete,
        missing_required=list(extraction.missing_required),
        warnings=list(extraction.warnings),
        vessel_name=_header_value(header, VESSEL),
        imo=_header_value(header, IMO),
        delivery_port=_header_value(header, PORT),
        header=header,
    )


def _delivery_row(delivery: RecordedDelivery) -> EmailDelivery:
    return EmailDelivery(**delivery.model_dump())


async def _replace_lines(
    session: AsyncSession, row: Email, matching: Sequence[RecordedMatch]
) -> None:
    """Every line of the RFQ, and what each was offered.

    Replaced whole: matching runs once per email and answers about all of them,
    so a second run is a different answer rather than an addition to this one.

    Cleared and flushed before the new ones are attached. Without the flush the
    unit of work issues the inserts first and Postgres refuses them - the pair
    (email_id, index) is unique, and line 1 of the second run arrives while
    line 1 of the first is still there.
    """
    row.lines.clear()
    await session.flush()

    row.lines = [
        RfqLine(
            index=line.index,
            verbatim=line.verbatim,
            description=line.description,
            customer_code=line.customer_code,
            quantity=line.quantity,
            uom=line.uom,
            item_code=line.item_code,
            item_description=line.item_description,
            item=dict(line.item),
            confidence=line.confidence,
            how=line.how,
            why=line.why,
            candidates=[
                RfqLineCandidate(
                    rank=rank,
                    item_code=one.item_code,
                    description=one.description,
                    confidence=one.confidence,
                    item=dict(one.item),
                )
                for rank, one in enumerate(line.candidates, start=1)
            ],
        )
        for line in matching
    ]


# --- the pipeline's models out of rows -------------------------------------


def _record(row: Email) -> EmailRecord:
    """One email, put back together in the shape the API renders."""
    attachments = [one for one in row.files if one.role is FileRole.ATTACHMENT]
    form = next((one for one in row.files if one.role is FileRole.FORM), None)

    return EmailRecord(
        id=row.id,
        decision_id=row.decision_id,
        message_id=row.message_id,
        source=row.source,
        received_at=row.received_at,
        recorded_at=row.created_at,
        updated_at=row.updated_at,
        mailbox=row.mailbox,
        sender=RecordedAddress(name=row.sender_name, address=row.sender_address)
        if row.sender_address or row.sender_name
        else None,
        to=[RecordedAddress(**one) for one in row.recipients],
        cc=[RecordedAddress(**one) for one in row.cc],
        subject=row.subject,
        body_chars=row.body_chars,
        body_file=BODY if row.body_key else None,
        attachments=[_file(one) for one in attachments],
        labels=list(row.labels),
        labelled=row.labelled,
        verdict=RecordedVerdict.model_validate(row.verdict, from_attributes=True)
        if row.verdict
        else None,
        extraction=RecordedExtraction.model_validate(row.extraction, from_attributes=True)
        if row.extraction
        else None,
        matching=[_line(one) for one in row.lines],
        delivery=RecordedDelivery.model_validate(row.delivery, from_attributes=True)
        if row.delivery
        else None,
        form=_file(form) if form else None,
        error=row.error,
    )


def _file(row: EmailFile) -> RecordedFile:
    return RecordedFile(
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        saved_as=row.saved_as,
        note=row.note,
    )


def _line(row: RfqLine) -> RecordedMatch:
    return RecordedMatch(
        index=row.index,
        verbatim=row.verbatim,
        description=row.description,
        customer_code=row.customer_code,
        quantity=row.quantity,
        uom=row.uom,
        item_code=row.item_code,
        item_description=row.item_description,
        confidence=row.confidence,
        item=dict(row.item),
        how=row.how,
        why=row.why,
        candidates=[
            RecordedCandidate(
                item_code=one.item_code,
                description=one.description,
                confidence=one.confidence,
                item=dict(one.item),
            )
            for one in row.candidates
        ],
    )


# --- small things ----------------------------------------------------------


def _header_value(header: dict, field: str) -> str | None:
    """What the header reader put under this field, whatever shape it used.

    `{"value": "Jebel Ali", "raw": "Jebel Ali", "source": "email.body"}` is what
    it writes today, and a bare string is what it wrote before that. Both read.
    """
    found = header.get(field)
    if isinstance(found, dict):
        found = found.get("value") or found.get("raw")
    return str(found) if found else None


def _name_of(value: object) -> str | None:
    address = _address(value)
    return address.name if address else None


def _address_of(value: object) -> str | None:
    address = _address(value)
    return address.address if address else None


def _wire(value: object) -> dict:
    address = _address(value)
    return address.model_dump() if address else {}
