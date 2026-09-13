"""One folder per email: what arrived, and what the agent did with it.

    Database/
    └── 2026-09-10T14-22-31Z__a1b2c3d4/
        ├── email.json                the message, the verdict, the delivery
        ├── body.txt                  the body as text, readable without a parser
        ├── Requisition.xlsx          the customer's files, exactly as they arrived
        └── KASS_RFQ_ALMI_HYDRA.xlsx  the copy of the desk's form we filled

One folder, one flat list of files: opening it shows the email and everything
that came with it, with nothing to descend into. `email.json` is what says
which file is whose - each attachment carries its own `saved_as`, and so does
the form.

The journal beside this - `data/decisions.jsonl` - stays what it has always
been: append-only, three lines per email, the account of *how* each answer was
reached. A record is the opposite and deliberately so: **one file per email,
rewritten** as the pipeline learns more, because "what happened to this email?"
is a question a page has to answer without folding three lines together.

Nothing here is redacted. The folder holds real customer mail and real customer
files, which is why `Database/` is not in git and why the API in front of it
maps field by field rather than serving these files as they are.
"""

import asyncio
import logging
import os
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.infrastructure.documents.loader import SourceFile
from src.infrastructure.storage.changes import Changes

logger = logging.getLogger(__name__)

RECORD = "email.json"
BODY = "body.txt"
# Ours, and the two names an attachment may not take: the customer chooses
# their own filenames, and one called `email.json` must not land on the record.
RESERVED = frozenset({RECORD.lower(), BODY.lower()})

# What a filename may contain once it is ours. A customer's attachment name is
# untrusted input that becomes a path, so everything outside this set goes -
# and the name is rebuilt from its last segment, never from the string as sent.
UNSAFE = re.compile(r"[^\w.()\- ]+", re.UNICODE)
MAX_NAME_CHARS = 120

# Why an attachment has no bytes on disk. Three different things, and an
# operator looking at an empty folder has to be able to tell them apart.
A_LINK = "A OneDrive or SharePoint link - there are no bytes to keep"
NOT_DOWNLOADED = "Not downloaded: the rules answered this email without opening it"
DOWNLOAD_FAILED = "Could not be downloaded from the mailbox"
NOT_KEPT = "Downloaded and read, but DATABASE_KEEP_ATTACHMENTS is off"
BACKFILLED = "Named in the journal, which never held the file itself"


class RecordedAddress(BaseModel):
    name: str | None = None
    address: str | None = None


class RecordedFile(BaseModel):
    """One attachment, as it arrived."""

    filename: str
    content_type: str | None = None
    size_bytes: int = 0
    # Path relative to this record's folder, or None when the bytes never
    # reached us. `note` says which of the reasons it was.
    saved_as: str | None = None
    note: str | None = None


class RecordedVerdict(BaseModel):
    """What the agent decided this email is."""

    category: str
    is_rfq: bool
    requires_action: bool
    recommended_action: str
    direction: str
    priority: str
    confidence: float
    needs_human_review: bool
    decision_path: str
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)
    rule_hits: list[str] = Field(default_factory=list)
    model: str | None = None


class RecordedExtraction(BaseModel):
    """What was read out of the RFQ, in the shape a page can show."""

    items: int = 0
    complete: bool = False
    missing_required: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # The header as the form spells it - vessel, port, dates. Free-form on
    # purpose: it grows when the form does, and nothing here is load-bearing.
    header: dict[str, Any] = Field(default_factory=dict)


class RecordedCandidate(BaseModel):
    """One product the search offered for a line, and how far behind it ranked."""

    item_code: str
    description: str = ""
    # 0-100, this candidate's search score as a percentage of the best one on
    # the same line. Not a probability, and not comparable between lines.
    confidence: int = 0
    # The whole row of the sheet, as for a match: the table shows a candidate
    # in the same columns it shows a confirmed product, and it may not have
    # fewer of them just because nothing has been confirmed yet.
    item: dict[str, Any] = Field(default_factory=dict)


class RecordedMatch(BaseModel):
    """One line of an RFQ, and the product it was matched to.

    `item` is the whole row of the product sheet, every column of it. The
    alternative was picking the fields that look useful today - and the sheet
    grows columns faster than this model would be updated, so the choice is
    between keeping everything and losing whatever nobody thought of.
    """

    index: int
    # As the reader got it out of the file, and as we said it back. Both,
    # because a match that turns out wrong is explained by the difference.
    verbatim: str = ""
    description: str = ""
    customer_code: str | None = None
    # As the customer wrote them, unconverted. The sheet has quantities and
    # units of its own; these are not those.
    quantity: str | None = None
    uom: str | None = None

    item_code: str | None = None
    # Our own wording for it, taken from whichever column the catalogue is
    # configured to describe products by. Resolved here so that nothing
    # downstream has to know the name of a column in somebody's spreadsheet.
    item_description: str = ""
    confidence: int | None = None
    item: dict[str, Any] = Field(default_factory=dict)
    # `code_confirmed`, `code_rejected`, `search` or `none`. The first thing an
    # operator looks at: "their code was wrong" and "we found it by its words"
    # are different things to be told.
    how: str = "none"
    why: str = ""
    # What the model was shown, scored. A wrong answer is only reviewable
    # against the list it was chosen from, and a refusal only means something
    # beside what it refused.
    candidates: list[RecordedCandidate] = Field(default_factory=list)


class RecordedDelivery(BaseModel):
    """Where the RFQ went, or why it did not go."""

    outcome: str
    forwarded_to: str | None = None
    cc: list[str] = Field(default_factory=list)
    region: str | None = None
    region_rule: str | None = None
    attached: str | None = None


class EmailRecord(BaseModel):
    """Everything known about one email, in one file."""

    id: str
    # Ties this folder to `data/decisions.jsonl` and to the filled workbook,
    # both of which are named after the decision.
    decision_id: str | None = None
    message_id: str | None = None
    source: str = "outlook"

    received_at: datetime | None = None
    recorded_at: datetime
    updated_at: datetime

    mailbox: str | None = None
    sender: RecordedAddress | None = None
    to: list[RecordedAddress] = Field(default_factory=list)
    cc: list[RecordedAddress] = Field(default_factory=list)
    subject: str | None = None
    body_chars: int = 0
    body_file: str | None = None

    attachments: list[RecordedFile] = Field(default_factory=list)
    # The Outlook categories stamped on the message. `labelled` is whether
    # Outlook took them: false means the mailbox copy carries no mark, which is
    # also what stops this email being forwarded twice.
    labels: list[str] = Field(default_factory=list)
    labelled: bool | None = None

    verdict: RecordedVerdict | None = None
    extraction: RecordedExtraction | None = None
    # One entry per line of the RFQ, matched or refused. Empty when the RFQ was
    # never read, or when matching is switched off.
    matching: list[RecordedMatch] = Field(default_factory=list)
    delivery: RecordedDelivery | None = None
    form: RecordedFile | None = None
    # Set when the pipeline raised on this email. The record exists either way:
    # an email that broke the agent is the one somebody most needs to see.
    error: str | None = None


class EmailRecords:
    """The `Database/` folder: writes a record per email, reads them back."""

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool,
        keep_attachments: bool = True,
        changes: Changes | None = None,
    ) -> None:
        self._root = root
        self._enabled = enabled
        self._keep = keep_attachments
        # Announced to after every write. The store does not know who listens
        # or what they do about it - only that the folder is not what it was.
        self._changes = changes or Changes()
        # One writer at a time. Two emails arriving together interleave on
        # every await in the pipeline, and a half-written email.json is a row
        # the page cannot render.
        self._lock = asyncio.Lock()

        if enabled:
            root.mkdir(parents=True, exist_ok=True)
            logger.info("Emails are recorded under %s", root)

    @classmethod
    def disabled(cls) -> "EmailRecords":
        """A store that writes nothing, so a caller needs no null checks."""
        return cls(Path("Database"), enabled=False)

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
        """Start a record the moment there is a verdict. Returns its id.

        Called from triage, so every entry point records - the webhook and the
        classify endpoint both. What happens to the email afterwards is added
        by `update`; at this point nothing has been sent or labelled yet.

        `files_note` is what the record says beside an attachment that has no
        bytes on it yet. It defaults to the only reason there is during a live
        run - nothing has been downloaded at this point - and the backfill tool
        says its own, because a journal line never held a file to begin with.
        """
        if not self._enabled:
            return None

        now = _now()
        record = EmailRecord(
            # Replaced below with the folder that was actually made: two
            # emails may want the same name, and only one of them can have it.
            id="",
            decision_id=decision_id,
            message_id=email.message_id,
            source=source,
            received_at=email.received_at,
            recorded_at=now,
            updated_at=now,
            mailbox=email.mailbox,
            sender=_address(email.sender),
            to=[_address(item) for item in email.to if item],
            cc=[_address(item) for item in email.cc if item],
            subject=email.subject,
            body_chars=len(email.body_text),
            body_file=BODY if email.body_text else None,
            # Names and sizes only: the bytes arrive with `update`, if the
            # pipeline downloaded them at all.
            attachments=[
                RecordedFile(
                    filename=item.filename,
                    content_type=item.content_type,
                    size_bytes=item.size_bytes or 0,
                    note=files_note,
                )
                for item in email.attachments
            ],
            verdict=_verdict(outcome),
        )

        try:
            async with self._lock:
                folder = self._make_folder(_record_id(email.received_at or now, decision_id))
                record.id = folder.name
                if email.body_text:
                    (folder / BODY).write_text(email.body_text, encoding="utf-8")
                _write(folder / RECORD, record)
        except Exception:
            # A record that cannot be written must not cost the email. The page
            # will be missing a row; the journal and the log still have it.
            logger.exception("Could not record %s", record.id)
            return None

        self._changes.announce()
        return record.id

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

        Rewrites `email.json` rather than appending to it - this file is the
        current state of one email, and the append-only account of how it got
        there is the journal's job.
        """
        if not self._enabled or record_id is None:
            return

        try:
            async with self._lock:
                folder = self._root / record_id
                record = _read(folder / RECORD)
                if record is None:
                    logger.warning("No record %s to update", record_id)
                    return

                if files is not None:
                    record.attachments = self._save_files(folder, files)
                if labels is not None:
                    record.labels = list(labels)
                if labelled is not None:
                    record.labelled = labelled
                if extraction is not None:
                    record.extraction = extraction
                if matching is not None:
                    record.matching = list(matching)
                if delivery is not None:
                    record.delivery = delivery
                if form is not None:
                    record.form = self._save_form(folder, *form, taken=_taken(record))
                if error is not None:
                    record.error = error

                record.updated_at = _now()
                _write(folder / RECORD, record)
        except Exception:
            logger.exception("Could not update record %s", record_id)
            return

        self._changes.announce()

    # --- reading ----------------------------------------------------------

    def all(self) -> Iterator[EmailRecord]:
        """Every record, newest first. Unreadable folders are skipped, loudly."""
        if not self._root.exists():
            return

        for folder in sorted(self._root.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            record = _read(folder / RECORD)
            if record is None:
                logger.warning("Skipping %s: no readable %s in it", folder.name, RECORD)
                continue
            yield record

    def read(self, record_id: str) -> EmailRecord | None:
        """One record by id, or None when there is no such folder."""
        folder = self._folder(record_id)
        return _read(folder / RECORD) if folder else None

    def file(self, record_id: str, relative: str) -> Path | None:
        """A file inside one record, or None when it is not there.

        `relative` comes off a record and therefore from a URL. It is resolved
        against the record's folder and refused if it lands anywhere else, so
        that `../../.env` is a 404 rather than a file.
        """
        folder = self._folder(record_id)
        if folder is None:
            return None

        target = (folder / relative).resolve()
        if not target.is_file() or folder.resolve() not in target.parents:
            return None
        return target

    # --- the disk ---------------------------------------------------------

    def _folder(self, record_id: str) -> Path | None:
        """The folder for an id, or None when the id is not one of ours.

        The id reaches this from a URL, so it is checked as a single safe
        segment before it is joined onto anything.
        """
        if not record_id or record_id != _safe_name(record_id):
            return None
        folder = self._root / record_id
        return folder if folder.is_dir() else None

    def _make_folder(self, wanted: str) -> Path:
        """A folder of its own for a new record, never somebody else's.

        Two emails arriving in the same second whose decision ids begin alike
        would otherwise be handed the same name, and the second would overwrite
        the first - the one way this store could lose an email outright.
        """
        folder = self._root / wanted
        index = 2
        while folder.exists():
            folder = self._root / f"{wanted}-{index}"
            index += 1
        folder.mkdir(parents=True)
        return folder

    def _save_files(self, folder: Path, files: Sequence[SourceFile]) -> list[RecordedFile]:
        """Write the attachments as they arrived, and say what became of each."""
        taken = set(RESERVED)
        saved = []

        for item in files:
            entry = RecordedFile(
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
                (folder / name).write_bytes(item.data)
                entry.saved_as = name
            saved.append(entry)

        return saved

    def _save_form(
        self, folder: Path, filename: str, data: bytes, *, taken: set[str]
    ) -> RecordedFile:
        """Keep the filled form beside the email it was made from.

        A second copy - `data/workbooks/` holds one named after the decision -
        and worth its 14 KB: the point of a record is that one folder answers
        everything about one email without a lookup.

        `taken` is what the folder already holds, because our own file must not
        overwrite one of the customer's - however unlikely the name collision.
        """
        name = _unique(_safe_name(filename), taken)
        (folder / name).write_bytes(data)
        return RecordedFile(filename=filename, size_bytes=len(data), saved_as=name)


def _taken(record: EmailRecord) -> set[str]:
    """Filenames already spoken for inside this record's folder."""
    return set(RESERVED) | {
        item.saved_as.lower() for item in record.attachments if item.saved_as
    }


def _verdict(outcome: ClassificationOutcome | None) -> RecordedVerdict | None:
    if outcome is None:
        return None
    result = outcome.result
    return RecordedVerdict(
        category=result.category.value,
        is_rfq=result.is_rfq,
        requires_action=result.requires_action,
        recommended_action=result.recommended_action.value,
        direction=result.direction.value,
        priority=result.priority.value,
        confidence=result.confidence,
        needs_human_review=result.needs_human_review,
        decision_path=result.decision_path.value,
        reasoning=result.reasoning,
        evidence=list(result.evidence),
        rule_hits=list(result.rule_hits),
        model=outcome.model,
    )


def _address(value: Any) -> RecordedAddress | None:
    if value is None:
        return None
    return RecordedAddress(name=value.name, address=value.address)


def _write(path: Path, record: EmailRecord) -> None:
    """Write the record whole or not at all.

    A page may be reading this file at the moment the pipeline finishes with
    the email, and a half-written JSON is a row that renders as an error.
    """
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read(path: Path) -> EmailRecord | None:
    if not path.is_file():
        return None
    try:
        return EmailRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Could not read %s", path)
        return None


def _record_id(when: datetime, decision_id: str | None) -> str:
    """`2026-09-10T14-22-31Z__a1b2c3d4` - sortable in a file browser, unique.

    The time comes first because a folder listing is then a run in order, and
    the decision id's head is what pairs the folder with the journal line.
    """
    stamp = when.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    tail = (decision_id or str(uuid4())).split("-")[0][:8]
    return f"{stamp}__{tail}"


def _safe_name(name: str) -> str:
    """A customer's filename, made safe to join onto a path.

    Taken from the last segment of whatever arrived - "..\\..\\.env" and
    "C:\\Users\\x\\rfq.xlsx" both name a file, not a place to write one.
    """
    last = re.split(r"[\\/]", name)[-1].strip()
    cleaned = UNSAFE.sub("_", last).strip(". ")
    return cleaned[:MAX_NAME_CHARS] or "attachment"


def _unique(name: str, taken: set[str]) -> str:
    """Two attachments may share a name, and neither may overwrite the other."""
    candidate = name
    stem, _, suffix = name.rpartition(".")
    index = 2
    while candidate.lower() in taken:
        candidate = f"{stem} ({index}).{suffix}" if stem else f"{name} ({index})"
        index += 1
    taken.add(candidate.lower())
    return candidate


def _now() -> datetime:
    return datetime.now(UTC)
