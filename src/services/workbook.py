"""Stage C: what was read out of the RFQ, written into a copy of the template.

No model runs here and nothing is decided here. Every value was already read,
verified and mapped onto what the workbook accepts; this stage only knows which
cell each of them belongs in. That division is deliberate - a cell address is
the one thing in this pipeline that never depends on what arrived in the email.

Two rules from the task shape the whole module:

* **Every cell this stage owns is written, blank included.** The master ships
  demo values in `C5`, `C10`, `H2` and `H8`, and a copy that inherits `AED`
  because nobody found a currency is a copy that invented one.
* **Rows go down in the customer's order.** `Sr. No` is the position in that
  order, and the loop below is the whole of it.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from src.infrastructure.documents.models import (
    ATTACHMENT_HAS_NO_BYTES,
    ATTACHMENT_IS_A_LINK,
    FILE_TOO_LARGE,
    NO_READER_FOR_KIND,
    PASSWORD_PROTECTED,
    TOO_MANY_ATTACHMENTS,
    TOTAL_SIZE_EXCEEDED,
    UNREADABLE,
)
from src.infrastructure.excel import (
    BLANK,
    CellValue,
    Day,
    Moment,
    Number,
    Template,
    Text,
)
from src.infrastructure.storage.workbooks import WorkbookStore
from src.services.extraction import RfqExtraction
from src.services.extraction.models import (
    DATE_AMBIGUOUS,
    DATE_IMPOSSIBLE,
    DATE_UNREADABLE,
    IMO_INVALID,
    NOTHING_TO_READ,
    READ_FAILED,
    HeaderField,
    LineItem,
)

logger = logging.getLogger(__name__)

# Every field read out of the RFQ, and the cell it fills. `A1` is not in here:
# in the master it is a formula deriving the customer's name from the sender
# code, and in the form we send it is the text that formula falls back to, which
# is what the desk's own finished RFQs carry.
CELLS: dict[HeaderField, str] = {
    HeaderField.VESSEL_NAME: "C2",
    HeaderField.IMO: "C3",
    HeaderField.RFQ_REFERENCE: "C4",
    HeaderField.CUSTOMER_CONTACT: "C6",
    HeaderField.CUSTOMER_PHONE: "C7",
    HeaderField.CUSTOMER_EMAIL: "C8",
    HeaderField.PERSON_DESIGNATION: "C9",
    HeaderField.DELIVERY_PORT: "H2",
    HeaderField.ETA: "H3",
    HeaderField.ETD: "H4",
    HeaderField.QUOTE_BEFORE: "H5",
    HeaderField.REQUESTED_DELIVERY: "H6",
    HeaderField.DELIVERY_ADDRESS: "H7",
    HeaderField.CURRENCY: "H8",
    HeaderField.SENDER_CODE: "H9",
    HeaderField.RFQ_TYPE: "H11",
}

# What the form itself calls each field the desk is ever told about by hand.
LABELS: dict[HeaderField, str] = {
    HeaderField.VESSEL_NAME: "Vessel Name",
    HeaderField.IMO: "IMO Number",
    HeaderField.RFQ_REFERENCE: "RFQ Reference",
    HeaderField.DELIVERY_PORT: "Delivery Port",
    HeaderField.CURRENCY: "Currency",
    HeaderField.RFQ_TYPE: "RFQ Type",
    HeaderField.ETA: "E.T.A",
    HeaderField.ETD: "E.T.D",
    HeaderField.QUOTE_BEFORE: "Quote Before",
    HeaderField.REQUESTED_DELIVERY: "Requested Delivery",
}

# What a filled copy is called on the wire. A plain spreadsheet, not a
# macro-enabled one: the form carries no macro, and saying it does is what makes
# a mail gateway strip the attachment.
CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The cells nobody reads out of the email. The branch comes from the same region
# rules that route the message, and it has to be right: the delivery-port
# dropdown in `H2` validates against whichever list `C5` names.
BRANCH_CELL = "C5"
SUBJECT_CELL = "C10"

# When the RFQ reached us. Two cells, the same value: in the master `H10` is a
# formula over `H12`, and in the form the desk sends they are two plain
# timestamps holding the same moment. Both finished RFQs we were given are like
# that, so both are written.
RECEIVED_CELLS = ("H10", "H12")

# The zone those two are written in. UTC by the desk's own choice; a spreadsheet
# cell carries no zone, so whoever reads it has to be told which one it is - and
# an email that arrives at 16:33 in Singapore reads 08:33 here.
DEFAULT_TIMEZONE = UTC

# Where the remarks go: a merged block of eight rows by ten columns, directly
# under the form and empty in the master and in every finished RFQ. Anything we
# could not fill in is explained here rather than in a value cell, because a
# value cell with prose in it breaks whatever reads the form next.
REMARKS_CELL = "A14"
MAX_REMARKS = 12

# When the customer gave no reference of their own. A starred cell, so it
# cannot be left empty, and it has to be recognisable as ours at a glance -
# nobody should mistake it for a number the customer quoted.
GENERATED_REFERENCE = "POC-{day}-{tail}"


class Subject(StrEnum):
    """What `C10` says this document is.

    An enum rather than a constant because the workbook offers two today and
    the team expects more. The values are checked against the master's own list
    at startup, so a new one there shows up as a warning here rather than as a
    cell Excel refuses.
    """

    REQUEST_FOR_QUOTE = "REQUEST FOR QUOTE"
    ORDER = "ORDER"

# The item grid: heading on row 23, first row of data on 24, and the ceiling the
# workbook's own macro works to (`F24:F5000`).
FIRST_ITEM_ROW = 24
LAST_ITEM_ROW = 5000
# Columns C and G to J - internal item code, prices, supplier - are not written.
# They belong to a later matching step, and the task says to leave them empty.
ITEM_COLUMNS = {"sr_no": "A", "customer_item_code": "B", "description": "D",
                "quantity": "E", "uom": "F"}

ITEMS_TRUNCATED = "items_did_not_fit_the_template"
REFERENCE_GENERATED = "rfq_reference_generated_the_customer_gave_none"

# Cells the desk's own forms hold as numbers rather than as text. Measured, not
# assumed: `C3` and `B` are numeric cells in both finished RFQs we were given.
NUMERIC_FIELDS = frozenset({HeaderField.IMO})

_PLAIN_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")

# Characters a filename cannot carry through a mail system.
_UNUSABLE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_MAX_NAME_PART = 60


@dataclass(frozen=True, slots=True)
class FilledWorkbook:
    """One populated copy of the template, ready to be attached to a forward."""

    data: bytes
    filename: str
    items: int
    template_checksum: str
    # Starred cells that went out empty. What decides whether the forward
    # carries a caveat, and what the caveat says.
    missing_required: list[str] = field(default_factory=list)
    # Attachments that arrived and gave nothing, already worded for a person.
    # Carried out of here because the desk reads the forwarded email before it
    # opens the form, and a file nobody could read is the first thing they need
    # to know about.
    unread: list[str] = field(default_factory=list)
    # Two files disagreeing about a quantity. Out here for the same reason, and
    # it is the more expensive of the two: a doubled row is a doubled order.
    conflicts: list[str] = field(default_factory=list)
    # Every empty cell with the reason it is empty, already worded for a
    # person. The forwarded email prints these, so that what the desk reads in
    # the email and what it reads on the form are the same sentences.
    why_blank: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Where the copy was kept, when it was kept at all.
    saved_to: Path | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.items) and not self.missing_required

    def journal_payload(self) -> dict[str, object]:
        """What the decision log keeps about the file that was produced.

        The checksum of the master goes in as well as the size of the copy: a
        workbook that came out wrong has to be traceable to the template it came
        from, and that template is a 13 MB binary somebody may have replaced.
        """
        return {
            "filename": self.filename,
            "size_bytes": len(self.data),
            "items": self.items,
            "complete": self.is_complete,
            "missing_required": self.missing_required,
            "why_blank": self.why_blank,
            "unread": self.unread,
            "conflicts": self.conflicts,
            "template_sha256": self.template_checksum,
            "warnings": self.warnings,
            "saved_to": str(self.saved_to) if self.saved_to else None,
        }


class WorkbookBuilder:
    """Turns an extraction into a filled copy of the master workbook."""

    def __init__(
        self,
        template: Template,
        store: WorkbookStore | None = None,
        *,
        timezone: tzinfo = DEFAULT_TIMEZONE,
        remarks: bool = True,
    ) -> None:
        self._template = template
        self._store = store
        self._timezone = timezone
        # The desk's own finished RFQs leave the block under the form empty, so
        # this can be turned off - but then the only place a flag appears is the
        # forwarded email and the journal, and the form itself says nothing.
        self._remarks = remarks

    async def build(
        self,
        extraction: RfqExtraction,
        *,
        branch: str | None = None,
        received_at: datetime | None = None,
        subject: Subject = Subject.REQUEST_FOR_QUOTE,
        keep_as: str | None = None,
    ) -> FilledWorkbook:
        """Fill one copy, and keep it if there is somewhere to keep it.

        The filling runs off the event loop: repacking the master is two seconds
        of compression, and the mailbox has other messages.
        """
        moment = received_at.astimezone(self._timezone) if received_at else None
        reference = _reference(extraction, moment, keep_as)
        cells, items, warnings = _contents(
            extraction, branch, moment, subject, reference, remarks=self._remarks
        )
        data = await asyncio.to_thread(self._template.fill, cells)
        filename = f"{_usable(reference.value) or 'KASS RFQ'}.xlsx"

        saved_to = None
        if self._store is not None and keep_as:
            saved_to = await self._store.save(keep_as, data)

        logger.info(
            "Wrote %s: %d item(s), %d cell(s) filled%s",
            filename,
            items,
            sum(1 for value in cells.values() if value is not BLANK),
            ", kept" if saved_to else " (not kept - WORKBOOKS_ENABLED is off)",
        )
        return FilledWorkbook(
            data=data,
            filename=filename,
            items=items,
            template_checksum=self._template.checksum,
            missing_required=list(extraction.missing_required),
            unread=unread_files(extraction),
            conflicts=list(extraction.conflicts),
            why_blank=why_blank(extraction, branch),
            warnings=warnings,
            saved_to=saved_to,
        )


def _contents(
    extraction: RfqExtraction,
    branch: str | None,
    received_at: datetime | None,
    subject: Subject,
    reference: "Reference",
    *,
    remarks: bool = True,
) -> tuple[dict[str, CellValue], int, list[str]]:
    """Every cell this stage owns, and what goes in it."""
    cells: dict[str, CellValue] = {cell: BLANK for cell in CELLS.values()}
    cells[SUBJECT_CELL] = Text(subject.value)
    cells[BRANCH_CELL] = Text(branch) if branch else BLANK
    for cell in RECEIVED_CELLS:
        cells[cell] = Moment(received_at) if received_at else BLANK

    for name, value in extraction.normalized.text.items():
        if name in CELLS:
            cells[CELLS[name]] = (
                _number_or_text(value) if name in NUMERIC_FIELDS else Text(value)
            )
    for name, day in extraction.normalized.dates.items():
        if name in CELLS:
            cells[CELLS[name]] = Day(day)

    room = LAST_ITEM_ROW - FIRST_ITEM_ROW + 1
    written = extraction.items[:room]
    for offset, item in enumerate(written):
        cells.update(_row(item, FIRST_ITEM_ROW + offset))

    cells[CELLS[HeaderField.RFQ_REFERENCE]] = Text(reference.value)
    if remarks:
        cells[REMARKS_CELL] = _remarks(extraction, branch, reference)

    warnings = [REFERENCE_GENERATED] if reference.generated else []
    if len(extraction.items) > room:
        logger.warning(
            "%d items but the template holds %d - the rest are not in the file",
            len(extraction.items),
            room,
        )
        warnings.append(ITEMS_TRUNCATED)

    return cells, len(written), warnings


def _row(item: LineItem, row: int) -> dict[str, CellValue]:
    """One line of the customer's list, in the position they put it in."""
    return {
        f"{ITEM_COLUMNS['sr_no']}{row}": Number(item.sr_no),
        f"{ITEM_COLUMNS['customer_item_code']}{row}": _code(item.customer_item_code),
        f"{ITEM_COLUMNS['description']}{row}": _written(item.description),
        f"{ITEM_COLUMNS['quantity']}{row}": _code(item.quantity),
        f"{ITEM_COLUMNS['uom']}{row}": _unit(item.uom),
    }


def _written(value: str | None) -> CellValue:
    """A value in a cell, exactly as it was read.

    Line breaks included. A description transcribed off a scan carries the
    page's layout with it - "WASHER PLAIN ROUND STEEL, M6.0..", then "-", then
    "IMPA:694815" on three lines - and collapsing that was tried and reverted:
    the desk's own finished RFQs hold multi-line descriptions and trailing
    spaces in exactly those cells, and `test_against_finished_rfqs` compares
    every one of them. Their file is the specification.
    """
    return Text(value) if value else BLANK


def _unit(value: str | None) -> CellValue:
    """The unit as the customer wrote it, in capitals.

    Not mapped onto the workbook's `GLOBAL UOM ID` table, deliberately. That
    table reads `BOX -> NULL` and `BTL -> UOM`, and both finished RFQs we have
    carry the customer's own spelling - `PC` on one row and `PCS` on the next.
    Capitals are the one thing the desk does do, and they lose nothing.
    """
    return Text(value.upper()) if value else BLANK


def _code(value: str | None) -> CellValue:
    return _number_or_text(value) if value else BLANK


def _number_or_text(value: str) -> CellValue:
    """A value that is plainly a number goes in as one; everything else as it is.

    Which is what the desk's own forms do - the IMO and the customer item code
    are numeric cells in both of them - and it lets the sheet add a quantity up.

    The leading-zero guard is the part that matters. `0012345` stays text,
    because a code somebody's system padded is not the number twelve thousand
    three hundred and forty-five. `1,5` stays text too: it is one and a half in
    half of Europe and one thousand five hundred in the other half, and this is
    not the place to decide which.
    """
    written = value.strip()
    if not _PLAIN_NUMBER.match(written):
        return Text(value)

    number = float(written)
    if number.is_integer() and str(int(number)) != written:
        # `007`, `1.0` written as `1.0` - the text says something the number
        # would not, so the text is what goes in.
        return Text(value)
    return Number(number)


@dataclass(frozen=True, slots=True)
class Reference:
    """The RFQ's reference number, and whether it is the customer's own.

    It names the file the desk searches by, so it also decides what the file is
    called: `PR/PM/26-27/01998` becomes `PR PM 26-27 01998.xlsx`, which is
    exactly the name the desk sent us.
    """

    value: str
    generated: bool = False


def _reference(
    extraction: RfqExtraction, received_at: datetime | None, decision_id: str | None
) -> Reference:
    """The customer's own reference, or one made up and marked as made up.

    A starred cell cannot be left blank, and the mapping document says to
    generate one. It is built from the day and the decision that produced it,
    so it is unique, it sorts, and nobody mistakes `POC-20260907-3F2A9C` for a
    number the customer quoted.
    """
    written = extraction.normalized.text.get(HeaderField.RFQ_REFERENCE)
    if written:
        return Reference(written)

    day = (received_at or datetime.now(UTC)).strftime("%Y%m%d")
    tail = (decision_id or uuid4().hex).replace("-", "")[:6].upper()
    return Reference(GENERATED_REFERENCE.format(day=day, tail=tail), generated=True)


def _usable(value: str | None) -> str:
    """A filename part: no separators, no runs of whitespace, not endless."""
    if not value:
        return ""
    return " ".join(_UNUSABLE.sub(" ", value).split())[:_MAX_NAME_PART].strip()


# What each reason means to somebody reading the form. Codes are for the
# journal; this is for the person who has to finish the RFQ by hand.
REASONS = {
    IMO_INVALID: "as given - the check digit does not add up",
    DATE_AMBIGUOUS: "could be read two ways",
    DATE_UNREADABLE: "not a date we could read",
    DATE_IMPOSSIBLE: "no such day in the calendar - check the customer's own file",
}

# A file that arrived and gave nothing, in words a person can act on. The
# codes are for the journal; these are for whoever has to finish the RFQ, and
# each one implies a different next move: open the email, ask the customer for
# another format, or unlock the file.
FILE_REASONS = {
    ATTACHMENT_IS_A_LINK: "a OneDrive link rather than a file - open it in the email",
    ATTACHMENT_HAS_NO_BYTES: "the mail server did not hand over its contents",
    NO_READER_FOR_KIND: "a format this agent cannot open",
    UNREADABLE: "the file is damaged",
    PASSWORD_PROTECTED: "locked with a password",
    FILE_TOO_LARGE: "over the size limit",
    TOTAL_SIZE_EXCEEDED: "the email was over the total size limit",
    TOO_MANY_ATTACHMENTS: "past the limit on how many files one email may carry",
    READ_FAILED: "the reader could not answer about it",
}
# Says only "there was nothing in it", so it is dropped whenever a code above
# says what was wrong. On its own it is the whole story.
NOTHING_IN_IT = "nothing could be read out of it"

QUANTITIES_DISAGREE = (
    "The same item at two different quantities - both rows are in the grid, "
    "check which one the customer meant:"
)
FILES_UNREAD = "Attached files nobody could read - check them by hand:"
COLUMNS_LEFT_OUT = "Columns of the customer's table that no cell of this form takes:"

FILLED_BY = "Filled automatically from the customer's email{files}."
NOTHING_READ = "No line items could be read - the grid below is empty."
NOT_FILLED = "Not filled in:"
PLEASE_CHECK = "Filled in, but worth checking:"
MADE_UP_REFERENCE = "RFQ reference generated - the customer quoted none."


def _remarks(
    extraction: RfqExtraction, branch: str | None, reference: "Reference"
) -> CellValue:
    """What a person needs to know about this copy, in the block under the form.

    Never in a value cell: prose in `H3` would be read as a date by whatever
    opens the form next. This block is empty in the master and in every
    finished RFQ the desk sent us, which is what makes it the right place.

    Blank when there is nothing to say - which never quite happens, because the
    first line always says a machine filled it in.
    """
    lines = [FILLED_BY.format(files=_files(extraction))]

    if reference.generated:
        lines.append(MADE_UP_REFERENCE)
    if not extraction.items:
        lines.append(NOTHING_READ)

    lines += _listed(PLEASE_CHECK, _flagged(extraction, branch))
    lines += _listed(NOT_FILLED, why_blank(extraction, branch))
    # Both of these are things that arrived and did not make it onto the form.
    # They used to live in the journal only, which means the person holding the
    # form could not know a file had been lost - and a silently dropped
    # requisition is the worst thing this agent can do.
    lines += _listed(QUANTITIES_DISAGREE, extraction.conflicts)
    lines += _listed(FILES_UNREAD, unread_files(extraction))
    lines += _listed(COLUMNS_LEFT_OUT, extraction.unmapped_columns)
    return Text("\n".join(lines))


def _listed(heading: str, entries: list[str]) -> list[str]:
    """One section of the block, or nothing when there is nothing in it."""
    if not entries:
        return []
    lines = [heading, *(f"  - {entry}" for entry in entries[:MAX_REMARKS])]
    if len(entries) > MAX_REMARKS:
        lines.append(f"  - and {len(entries) - MAX_REMARKS} more, see the forwarded email")
    return lines


def _flagged(extraction: RfqExtraction, branch: str | None) -> list[str]:
    """Cells that were filled in from a best guess rather than a certainty."""
    return [
        f"{label(name.value)}: {_reason(reason, extraction, branch)}"
        for name, reason in extraction.normalized.checked.items()
    ]


def why_blank(extraction: RfqExtraction, branch: str | None = None) -> list[str]:
    """Each cell that came out empty, with the reason it did.

    Two kinds of reason, and they never collide. Code answers for a value the
    customer wrote and we would not take - a date that reads two ways, an IMO
    whose check digit fails - because those refusals are exact. The model
    answers for a value that is simply not in the email, because it is the only
    thing that read the email and can say where it looked.

    Public because the forwarded email says the same thing as the block under
    the form: the desk reads one of the two, and they must not differ.
    """
    reasons = extraction.normalized.dropped
    starred = [HeaderField(name) for name in extraction.missing_required]
    # Every starred cell that came out empty, plus anything the customer did
    # write and we could not use - an unreadable ETA is not starred, and is
    # still the thing a person needs to know about.
    named = starred + [name for name in reasons if name not in starred]

    lines = []
    for name in named:
        detail = _reason(reasons.get(name, ""), extraction, branch) or (
            extraction.header.not_found.get(name, "")
        )
        lines.append(f"{label(name.value)}: {detail}" if detail else label(name.value))
    return lines


def unread_files(extraction: RfqExtraction) -> list[str]:
    """Every attachment that arrived and gave nothing, with the reason.

    A container is not one of them: a zip's or an email's contents became
    documents of their own, and the wrapper having nothing of its own to show
    is how it is supposed to work. Those carry no warning, which is what tells
    them apart from a file that failed.
    """
    named = []
    for read in extraction.documents:
        if read.holds_items or read.facts:
            continue
        codes = list(dict.fromkeys(read.document.warnings + read.warnings))
        told = [FILE_REASONS[code] for code in codes if code in FILE_REASONS]
        if told:
            named.append(f"{read.origin}: {', '.join(told)}")
        elif NOTHING_TO_READ in codes:
            named.append(f"{read.origin}: {NOTHING_IN_IT}")
    return named


def _reason(code: str, extraction: RfqExtraction, branch: str | None) -> str:
    """One warning code as a sentence somebody can act on."""
    return REASONS.get(code, "")


def _files(extraction: RfqExtraction) -> str:
    read = [item for item in extraction.documents if item.origin != "email.body"]
    return f" and {len(read)} attachment(s)" if read else ""


def label(field: str) -> str:
    """A field as the form names it, for a person to read."""
    try:
        return LABELS.get(HeaderField(field), field.replace("_", " ").title())
    except ValueError:
        return field
