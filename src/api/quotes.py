"""What the front end reads: one row per email the agent has seen.

The rows come out of `Database/`, one folder per email, and are shaped here
into the table the Quote Overview page already renders. That mapping is the
whole point of this module: a record holds the customer's own words, the
model's reasoning and every file they sent, and none of that may reach a
browser by accident. Fields are copied one at a time, and a field nobody asked
for is not on the wire.

Most columns are deliberately empty. The agent knows what an email is and what
it did with it; it does not know a quotation number, a responsible user or a
completion percentage, and inventing them would make a table that reads as
finished work. What it does know - the vessel, the port, how many line items it
read - is in the record, one line away from being shown.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from src.api.dependencies import CatalogDep, ChangesDep, RecordsDep
from src.domain.enums import DeliveryOutcome, EmailCategory, Priority
from src.domain.rules.catalog import Catalog, CatalogItem, normalize_code
from src.infrastructure.storage.changes import Changes
from src.infrastructure.storage.records import EmailRecord, RecordedMatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/quotes", tags=["Quotes"])

MAX_PAGE_SIZE = 500

# The most a single unit may be quoted at. Not a business rule - a guard
# against a number nobody meant to type.
MOST_A_UNIT_COSTS = 10_000_000.0

# How long the stream stays silent before it says something anyway. Proxies and
# load balancers close a connection that has sent nothing for a minute or two,
# and a comment line costs nothing and is ignored by every SSE client.
KEEPALIVE_SECONDS = 25.0
CHANGED = "event: quotes\ndata: changed\n\n"
KEEPALIVE = ": keep-alive\n\n"

# What the agent's verdict means in the table's own vocabulary.
#
# Only three of the eight statuses can be reached from here, and that is
# honest: the rest describe work a person does after the RFQ has been picked
# up - waiting for suppliers, sending a quotation - and the agent does none of
# it. Everything the agent has merely read and labelled is New; the label
# column beside it says what it actually is.
# The header fields this page shows, spelled as the extraction writes them.
VESSEL = "vessel_name"
IMO = "imo"
PORT = "delivery_port"

FORWARDED = "inProgress"
DEFAULT_STATUS = "new"
STATUS_BY_CATEGORY = {
    EmailCategory.CUSTOMER_ORDER_PO: "order",
    EmailCategory.CUSTOMER_CLARIFICATION: "replyReceived",
}


class Wire(BaseModel):
    """A model that goes out camelCased, because a TypeScript client reads it."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class QuoteRow(Wire):
    """One row of Quote Overview."""

    # Null while there is no detail page for an email: clicking the row must
    # not open one that cannot be filled in.
    id: str | None = None
    # The record's own id. Stable across refreshes, unique per email, and what
    # the table keys its rows by.
    row_key: str
    # The Outlook categories the agent stamped on the message, exactly as they
    # are spelled in the mailbox.
    labels: list[str] = Field(default_factory=list)

    status: str
    status_detail: str = ""
    customer_name: str

    quotation_number: str = ""
    reference: str = ""
    vessel_name: str = ""
    imo: str | None = None
    port: str = ""
    store_type: str | None = None
    product_category: str | None = None
    priority: str = "NORMAL"
    received_on: str = ""
    due_date: str = ""
    responsible_user: str = ""
    processing_time: str = ""
    # Null rather than zero: the agent has not counted the line items of an
    # email it never opened, and "we do not know" and "none" are different
    # answers in a column somebody prices work from. The shape is the client's
    # own, for the day these are filled in.
    counts: dict[str, int] | None = None
    completion_percent: int | None = None
    # How many lines the matching screen has for this RFQ - the rows of its
    # Product matching table, and nothing else. Separate from `counts`, which
    # wants a breakdown by priced, in stock and variants: the agent knows none
    # of those, and a breakdown of zeroes reads as work already done.
    line_count: int = 0


class QuotePage(Wire):
    """A page of rows, in the shape the repository on the other side expects."""

    items: list[QuoteRow]
    total: int
    page: int
    page_size: int


@router.get("", status_code=status.HTTP_200_OK)
async def list_quotes(
    records: RecordsDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, alias="pageSize")] = 25,
    search: str = "",
) -> QuotePage:
    """Every email the agent has recorded, newest first.

    Read from disk on each request rather than cached: a mailbox makes a few
    hundred records a day, the folder is local, and a cache that can be stale
    is a bug report about an email that "did not arrive" when it did.
    """
    rows = [_row(record) for record in await records.all() if _is_rfq(record)]

    needle = search.strip().lower()
    if needle:
        rows = [row for row in rows if needle in _haystack(row)]

    start = (page - 1) * page_size
    return QuotePage(
        items=rows[start : start + page_size],
        total=len(rows),
        page=page,
        page_size=page_size,
    )


@router.get("/stream")
async def stream_quotes(changes: ChangesDep) -> StreamingResponse:
    """Says "the list changed", so a page does not have to keep asking.

    One event, no payload: whatever happened, the answer is to read the list
    again, and a page that already does that on every filter needs nothing more
    from here. The browser's own EventSource reconnects on its own, so there is
    no retry logic on either side of this.

    Declared above `/{record_id}` on purpose - routes match in the order they
    are added, and "stream" would otherwise be taken for a record id.
    """
    return StreamingResponse(
        events(changes),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Nginx buffers a response body by default, which for a stream
            # means the page hears nothing until the buffer fills.
            "X-Accel-Buffering": "no",
        },
    )


async def events(changes: Changes) -> AsyncIterator[str]:
    """One line per change, a comment when there is nothing to say.

    Separate from the endpoint above so it can be read - and tested - as what
    it is: a loop that waits, and says one of two things.
    """
    with changes.subscribe() as changed:
        logger.info("A page is watching the list, %d in total", changes.listeners)
        while True:
            try:
                await asyncio.wait_for(changed.wait(), KEEPALIVE_SECONDS)
            except TimeoutError:
                yield KEEPALIVE
                continue
            changed.clear()
            yield CHANGED


class MatchCandidate(Wire):
    """One product the search offered for a line, with its whole sheet row."""

    item_code: str
    description: str = ""
    # 0-100, this candidate's search score as a percentage of the best one on
    # the same line. Shown to a person; nothing in the service decides on it.
    confidence: int = 0
    # Every column of the sheet's row, so that a candidate fills the same
    # columns of the table as a confirmed product does.
    item: dict[str, Any] = Field(default_factory=dict)


class MatchRow(Wire):
    """One line of an RFQ beside the product it was matched to.

    The left half is what the customer asked for, unconverted - their code,
    their words, their quantity in their own unit. The right half is ours. The
    two are kept apart on the wire as they are on the page, because the whole
    point of the screen is comparing them.
    """

    # Where it sits on the page, 1..n. What a person points at.
    line: int
    # What the record calls this line, which is the reader's own numbering
    # inside the file it came out of. The two are different on purpose, and
    # this is the one the confirmation endpoint below is addressed by: a screen
    # that renumbers rows must not re-point a confirmation at somebody else's
    # product.
    index: int = 0
    customer_code: str = ""
    customer_description: str = ""
    quantity: str = ""
    uom: str = ""

    item_code: str = ""
    item_description: str = ""
    # Everything else the sheet's row says about the product. The page does not
    # show it yet; the record has it, so the wire may as well carry it.
    item: dict[str, Any] = Field(default_factory=dict)
    confidence: int | None = None
    # `code_confirmed`, `code_rejected`, `search` or `none`.
    how: str = "none"
    why: str = ""
    candidates: list[MatchCandidate] = Field(default_factory=list)
    # The product a person settled on. Empty until somebody does - and empty
    # again once they change their mind, because those are one state.
    confirmed_item_code: str = ""
    # What a supplier quoted for one unit of this line. Null until one of them
    # answers; the quantity is the customer's, and the total is the screen's
    # arithmetic rather than anything a supplier said.
    offer_unit_price: float | None = None


class Confirmation(Wire):
    """Which product a person settled this line on."""

    item_code: str


class Offer(Wire):
    """What a supplier quoted for one unit of this line.

    Greater than zero, and no larger than a price anybody would put in a
    quotation. Both bounds are here rather than in the store because this is
    where a number arrives from outside: a zero would read as "free" on the
    pricing screen, and a typo with four extra digits would read as a quotation
    somebody has to explain.
    """

    unit_price: float = Field(gt=0, le=MOST_A_UNIT_COSTS)


class RfqDetail(Wire):
    """One RFQ as its own page reads it."""

    id: str
    # The desk's own number - `RFQ-0042`. Empty for a record opened before
    # numbering existed; the page then shows a dash rather than the id, which
    # addresses the record and was never meant to be read out loud.
    reference: str = ""
    customer_name: str = ""
    vessel_name: str = ""
    imo: str = ""
    port: str = ""
    received_on: str = ""
    subject: str = ""
    lines: list[MatchRow] = Field(default_factory=list)


@router.get("/{record_id}/rfq", status_code=status.HTTP_200_OK)
async def read_rfq(record_id: str, records: RecordsDep, catalog: CatalogDep) -> RfqDetail:
    """One RFQ, in the shape the matching screen reads.

    A view of the record rather than the record itself: `/{id}` below serves
    that, and it carries the model's reasoning, the customer's body text and
    every file name. This one carries the lines and nothing else.
    """
    record = await records.read(record_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such RFQ")

    return RfqDetail(
        id=record.id,
        reference=record.rfq_reference or "",
        customer_name=_who(record),
        vessel_name=_header(record, VESSEL),
        imo=_header(record, IMO),
        port=_header(record, PORT),
        received_on=_received(record),
        subject=record.subject or "",
        lines=[
            _line(number, match, catalog.current)
            for number, match in enumerate(record.matching, start=1)
        ],
    )


@router.put("/{record_id}/rfq/lines/{index}/confirmation", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_line(
    record_id: str, index: int, body: Confirmation, records: RecordsDep, catalog: CatalogDep
) -> Response:
    """Settle one line on one product.

    The product has to be **in the sheet**, and that is the whole of the check.
    Not "on this line's shortlist": the five candidates are a proposal, and a
    person who finds none of them right goes and picks the sixth by hand - that
    is what the picker is for. What may not happen is a code nobody sells
    reaching an order.

    404 rather than 400: from the caller's side "there is no such thing here"
    is the same answer whether the line is missing or the code is, and saying
    which would only tell a guesser that they guessed half right.
    """
    found = catalog.current.by_code(body.item_code)
    if found is None or normalize_code(found.code) != normalize_code(body.item_code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such product")

    if not await records.confirm(record_id, index, found.code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such line")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{record_id}/rfq/lines/{index}/confirmation", status_code=status.HTTP_204_NO_CONTENT)
async def unconfirm_line(record_id: str, index: int, records: RecordsDep) -> Response:
    """Un-settle one line. Idempotent: clearing what is already clear is fine."""
    if not await records.confirm(record_id, index, None):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such line")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{record_id}/rfq/lines/{index}/offer", status_code=status.HTTP_204_NO_CONTENT)
async def price_line(record_id: str, index: int, body: Offer, records: RecordsDep) -> Response:
    """Record what a supplier quoted for one unit of this line.

    No check against the sheet, unlike a confirmation: a price is the
    supplier's to name, and ours to believe or not. The only thing refused
    here is a number that cannot be one.
    """
    if not await records.price(record_id, index, body.unit_price):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such line")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{record_id}/rfq/lines/{index}/offer", status_code=status.HTTP_204_NO_CONTENT)
async def unprice_line(record_id: str, index: int, records: RecordsDep) -> Response:
    """Forget a supplier's price. Idempotent: clearing what is clear is fine."""
    if not await records.price(record_id, index, None):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such line")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{record_id}", status_code=status.HTTP_200_OK)
async def read_quote(record_id: str, records: RecordsDep) -> EmailRecord:
    """One email in full: the verdict, the reading, the delivery, the files.

    The record as it is on disk, which is more than the table shows and is
    meant to be - this is what a person opens when they want to know why an
    email was labelled the way it was.
    """
    record = await records.read(record_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such email")
    return record


@router.get("/{record_id}/files/{path:path}", status_code=status.HTTP_200_OK)
async def read_file(record_id: str, path: str, records: RecordsDep) -> Response:
    """One file out of a record: an attachment as it arrived, or the filled form.

    `path` is what the record itself printed under `savedAs`. The store refuses
    a name that is not one of this record's own, so a guessed path is a 404
    rather than somebody else's attachment.

    The bytes come back through here rather than from a link to storage. That
    is the whole reason the container is private: an attachment is a customer's
    file, and a URL anybody can follow is not a permission model.
    """
    data = await records.file(record_id, path)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")

    name = path.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _row(record: EmailRecord) -> QuoteRow:
    """One record, reduced to what a table may show.

    The sender's address stands in for the customer name. It is what we
    actually know: the agent reads a mailbox, not a customer registry, and a
    name guessed off a domain would be wrong for exactly the customers whose
    mail matters most.
    """
    return QuoteRow(
        # The record's own id opens its page. Every row on this list is an RFQ
        # the agent has read, and there is something to show for each of them.
        id=record.id,
        row_key=record.id,
        reference=record.rfq_reference or "",
        labels=list(record.labels),
        status=_status(record),
        customer_name=_who(record),
        vessel_name=_header(record, VESSEL),
        priority=_priority(record),
        line_count=len(record.matching),
    )


def _line(number: int, match: RecordedMatch, catalog: Catalog) -> MatchRow:
    """One matched line, as the page reads it.

    `line` is counted here rather than taken from the record: the reader
    numbers rows within the file it found them in, and a screen wants 1..n down
    the page. The record keeps its own numbering, which is what a warning about
    "row 14 of the requisition" refers to.

    A settled line is shown as what it was settled on. The record keeps only
    the code - the product it names is looked up in today's sheet, because the
    sheet is where a product's description, source and unit live, and a page
    showing last week's copy of them would be showing something nobody sells.
    """
    settled = _settled(match, catalog)
    if settled is not None:
        return _showing(_unsettled(number, match, catalog), settled, match)
    return _unsettled(number, match, catalog)


def _fields(catalog: Catalog, code: str, recorded: Mapping[str, Any]) -> dict[str, Any]:
    """One product's row of the sheet, as the sheet has it now.

    The record keeps the row it matched against, and that is the right copy for
    explaining a decision taken last month. It is the wrong one for a screen
    somebody is about to settle a line from: the supplier, the source and the
    unit are edited in the sheet, and last month's copy of them describes
    something nobody sells today.

    The recorded row is the fallback, for a code the sheet no longer carries.
    Fewer columns beats a candidate that goes blank.
    """
    found = catalog.by_code(code) if code else None
    # `by_code` answers to a customer's code as well as to ours, and a customer
    # code that happens to be somebody else's product would quietly swap the
    # row. Only our own code may replace it.
    if found is None or normalize_code(found.code) != normalize_code(code):
        return dict(recorded)
    return dict(found.fields)


def _settled(match: RecordedMatch, catalog: Catalog) -> CatalogItem | None:
    """The product this line was settled on, as the sheet has it now."""
    if not match.confirmed_item_code:
        return None
    found = catalog.by_code(match.confirmed_item_code)
    return found if found is not None and found.code == match.confirmed_item_code else None


def _showing(row: MatchRow, product: CatalogItem, match: RecordedMatch) -> MatchRow:
    """The same line, showing the product a person settled it on.

    Its score comes from the shortlist when it was on the shortlist, and is
    nothing when it was picked by hand: a person chose it, and the number was
    not the reason.
    """
    scored = next(
        (one.confidence for one in match.candidates if one.item_code == product.code), None
    )
    return row.model_copy(
        update={
            "item_code": product.code,
            "item_description": product.description,
            "item": dict(product.fields),
            "confidence": scored,
        }
    )


def _unsettled(number: int, match: RecordedMatch, catalog: Catalog) -> MatchRow:
    return MatchRow(
        line=number,
        index=match.index,
        customer_code=match.customer_code or "",
        customer_description=match.verbatim or match.query,
        quantity=match.quantity or "",
        uom=match.uom or "",
        item_code=match.item_code or "",
        item_description=match.item_description,
        item=_fields(catalog, match.item_code or "", match.item),
        confidence=match.confidence,
        how=match.how,
        why=match.why,
        confirmed_item_code=match.confirmed_item_code or "",
        offer_unit_price=match.offer_unit_price,
        candidates=[
            MatchCandidate(
                item_code=one.item_code,
                description=one.description,
                confidence=one.confidence,
                item=_fields(catalog, one.item_code, one.item),
            )
            for one in match.candidates
        ],
    )


def _received(record: EmailRecord) -> str:
    """The day the email arrived, as a date. Empty when nothing recorded one."""
    when = record.received_at or record.recorded_at
    return when.date().isoformat() if when else ""


def _is_rfq(record: EmailRecord) -> bool:
    """Whether this email is one the desk would call an RFQ.

    The list is RFQs, not mail. Everything else the agent classifies - a
    supplier's reply, an auto-reply, marketing - is in the record folder and on
    the journal line, and has no place on a page about quoting.
    """
    return record.verdict is not None and record.verdict.is_rfq


def _header(record: EmailRecord, field: str) -> str:
    """One field of the header the agent read off the RFQ.

    `value` is the form's own spelling of it and `raw` is the customer's; the
    first is what goes in the cell, so it is what is shown.
    """
    if record.extraction is None:
        return ""
    found = record.extraction.header.get(field) or {}
    return str(found.get("value") or found.get("raw") or "")


def _status(record: EmailRecord) -> str:
    """Where this email stands, in the table's vocabulary.

    An RFQ that reached its desk is in progress - somebody has it. Everything
    else the agent has classified is New until a person moves it.
    """
    if record.delivery and record.delivery.outcome == DeliveryOutcome.SENT.value:
        return FORWARDED
    if record.verdict is None:
        return DEFAULT_STATUS
    try:
        category = EmailCategory(record.verdict.category)
    except ValueError:
        # A record written by an older version, naming a category this one no
        # longer has. Showing the row as New beats dropping the email.
        logger.warning("Unknown category %r in %s", record.verdict.category, record.id)
        return DEFAULT_STATUS
    return STATUS_BY_CATEGORY.get(category, DEFAULT_STATUS)


def _priority(record: EmailRecord) -> str:
    """The one thing the verdict says that the table already has a place for."""
    if record.verdict and record.verdict.priority == Priority.URGENT.value:
        return "HIGH"
    return "NORMAL"


def _who(record: EmailRecord) -> str:
    """Who wrote it, as an address. Empty for a record with no sender at all -
    a pasted email often has none, and "unknown" is a worse answer than blank.
    """
    if record.sender and record.sender.address:
        return record.sender.address
    return ""


def _haystack(row: QuoteRow) -> str:
    """What `search` looks through. The sender and the labels, because those
    are the two columns that carry anything to search."""
    return " ".join([row.customer_name, *row.labels]).lower()
