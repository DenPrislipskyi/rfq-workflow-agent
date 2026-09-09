"""Everything the model is shown when it looks at one attachment.

Only a sample reaches it. The whole point of stage A was that code already holds
the file exactly; asking a model to read all 244 rows to answer "what is this?"
would cost a hundred times more and answer no better.

All untrusted content is wrapped in tags and labelled as data, the same way the
classifier does it.
"""

from typing import TYPE_CHECKING

from src.domain.models import NormalizedEmail, Signals
from src.infrastructure.documents.budget import truncate
from src.infrastructure.documents.models import Document, FileKind, Grid, ImageRef
from src.infrastructure.llm.client import ContentBlock, Messages, image_block
from src.services.extraction.table import TableBlock, find_blocks

if TYPE_CHECKING:  # `models` imports nothing from here; the quotes keep it that way.
    from src.services.extraction.models import ReadDocument
    from src.services.extraction.text_parts import Part

# A zip or an attached email holds no content of its own - what was inside it
# became documents of its own. Saying "nothing to read" about the wrapper is
# noise; a container that failed to unpack already says so itself.
CONTAINERS = frozenset({FileKind.ZIP, FileKind.SEVEN_ZIP, FileKind.EML, FileKind.MSG})

FILE_SYSTEM_PROMPT = """\
You are reading ONE file attached to an email sent to a marine ship chandler that
supplies stores, spares and provisions to vessels. The email is a Request For
Quotation: a shipping company asking the chandler to price a list of marine items.

Answer everything about this one file in one go.

## what

One sentence naming the file. "Requisition listing 244 deck items", "Photo of a
VHF radio showing its nameplate", "General arrangement drawing of a ballast pump".

## has_item_list

True ONLY if this file contains the list of goods the customer is asking us to
quote - a requisition, an enquiry list, an order form.

False for everything else, even when product identifiers appear on it: a drawing,
a photo, a specification sheet, a certificate, a class report, a covering letter.
A parts table printed on a drawing is not the customer's requisition.

When a file holds both a covering letter and the list, answer true.

## facts

Product identifiers VISIBLE in the file, copied EXACTLY as written: manufacturer,
model, type designation, part number, serial number, IMPA code, electrical
rating, dimensions, capacity.

Copy character for character. Never infer, never complete a partial number from
what it "should" be, never repeat every row of an item list here. This field is
for a file with no list, where the identifiers are the content.

## Then one of two, never both

### tables - when the file has tables and they hold the item list

You are NOT copying the rows. Code does that, exactly, as soon as it knows what
each column means. Getting the meaning right is the whole job.

Give one entry per table shown, with its number, the row its headings are on,
and what each column supplies:

- `sr_no` - the customer's own line numbering. Usually 1, 2, 3 with no gaps.
- `customer_item_code` - the customer's identifier: an IMPA or ISSA code, a part
  number, a catalogue reference. IMPA codes are 6 digits.
- `description` - what the article is. The longest text column by far.
- `quantity` - how many are wanted. Numeric in almost every row.
- `uom` - the unit the quantity is counted in: PCS, KG, LTR, CTN, coil, can.
  Few distinct values repeated over many rows.

Assign each field at most once per table, and only when you are confident. A
column you do not name is left out and reported - the correct outcome for
`REMARKS`, `MAKER`, prices, or anything with no field above.

Where a table shows a profile instead of all its rows, the profile counts every
row in the file. Trust it over the sample when they seem to differ.

The prompt names the row code believes the headings are on. If they are on a
different row, say so and use that number.

### items - when there is no table and the list is in the text or the pictures

Transcribe EVERY item, in the order they appear. Do not merge two into one and
do not split one into two.

Copy each value EXACTLY as written. "2 coils" stays "2 coils". "1,5" stays
"1,5". Do not convert, expand abbreviations, correct spelling or translate.

Most of these lists carry no item code at all: a description alone is a complete
item. Leave a field out rather than inventing it.

Ignore everything that is not an item: letterheads, addresses, prices, totals,
payment terms, compliance notices, signatures, page numbers.

Give each item the `page` label it came from, from the list in <parts>.

## Above all

Leave a field unassigned rather than guessing. A wrong quantity becomes a wrong
order; a missing one costs somebody one lookup.
"""

FILE_INSTRUCTION = "Read the file inside <file>."

# A table this size goes to the model whole: it is a couple of thousand tokens,
# and seeing every row answers better than any description of them. Past it the
# arithmetic reverses - a 244-row requisition is a hundred thousand tokens, and
# a profile measured over all 244 says more than any sample could.
WHOLE_TABLE_ROWS = 40
# Cells per sampled row. Wide enough for a real description, short enough that
# fifteen rows stay small.
MAX_SAMPLE_CELL_CHARS = 48


def build_file_messages(
    document: Document,
    blocks: list[TableBlock],
    parts: list["Part"],
    problem: str | None = None,
) -> Messages:
    """Everything the model is shown about one file.

    `problem` is set only on the retry after a mapping failed verification.
    """
    sections = [
        f"{FILE_INSTRUCTION}\n",
        "<file>",
        # Named, not offered as content: one model read a reference number off
        # the filename and reported it as the customer's own.
        f"filename (metadata, not content): {document.origin}",
        f"type: {document.kind.value}",
        f"size: {document.size_bytes} bytes",
    ]

    if blocks:
        sections.append(_tables(blocks))
    elif parts:
        sections.append(_first_part(parts))

    if document.warnings:
        # The model should know the sample is partial before calling a file empty.
        sections.append(f"\nreading warnings: {', '.join(document.warnings)}")
    sections.append("</file>")

    if problem:
        sections.append(
            f"\nYour previous answer did not hold up against the data: {problem}\n"
            "Look again and correct it."
        )

    text = "\n".join(sections)
    images = parts[0].images if parts and not blocks else []
    if not images:
        return [("system", FILE_SYSTEM_PROMPT), ("human", text)]

    blocks_out: list[ContentBlock] = [{"type": "text", "text": text}]
    blocks_out += [image_block(image.data, image.media_type) for image in images]
    return [("system", FILE_SYSTEM_PROMPT), ("human", blocks_out)]


def _tables(blocks: list[TableBlock]) -> str:
    """Each table, whole when it is small and as a profile when it is not."""
    out = []
    for number, block in enumerate(blocks, start=1):
        title = f" - {block.title}" if block.title else ""
        out.append(
            f"\n[table {number}{title}]  sheet {block.grid.name or '-'}, "
            f"header row {block.header_row + 1}, "
            f"data rows {block.first_data_row + 1} to {block.last_data_row + 1}"
            f" ({block.height} rows)"
        )
        out.append(_whole(block) if block.height <= WHOLE_TABLE_ROWS else _profiled(block))
    return "\n".join(out)


def _whole(block: TableBlock) -> str:
    """Every row. Small tables read better than they describe."""
    return _rows(block, list(block.data_rows()))


def _profiled(block: TableBlock) -> str:
    """What each column holds over every row, plus a spread of rows.

    The profile is what separates `QTY` from `PACK SIZE` - 60 distinct values
    against 4 - and what shows a `REMARKS` column filled in 7 rows out of 244.
    No sample taken from the top would show either.
    """
    return (
        "columns, measured over every data row:\n"
        + block.render_profiles()
        + "\n\nsample rows:\n"
        + _rows(block, block.sample_rows())
    )


def _rows(block: TableBlock, rows: list[tuple[int, list[str | None]]]) -> str:
    widths = {column.letter: _width(column) for column in block.profiles}
    header = " | ".join(f"{column.letter:>{widths[column.letter]}}" for column in block.profiles)
    lines = [f"  row |  {header}"]

    for number, cells in rows:
        values = " | ".join(
            f"{_sample_cell(cells, column.index):>{widths[column.letter]}}"
            for column in block.profiles
        )
        lines.append(f"{number + 1:>5} |  {values}")
    return "\n".join(lines)


def _first_part(parts: list["Part"]) -> str:
    """The first part of a document with no table: its text, and its pictures."""
    out = ["\n<parts>"]
    for label, text in parts[0].texts:
        out.append(f"\n[{label}]\n{text}")
    for image in parts[0].images:
        out.append(f"\n[{image.origin}] - attached below as an image")
    if len(parts) > 1:
        out.append(f"\n[{len(parts) - 1} further part(s) follow in later calls]")
    out.append("</parts>")
    return "\n".join(out)


def _width(column) -> int:
    """Capped, so a description of 84 characters cannot push a one-character
    quantity off the line."""
    return min(max(len(column.letter), column.longest, len(column.header or "")), 24)


def _sample_cell(cells: list[str | None], index: int) -> str:
    value = cells[index] if index < len(cells) else None
    if not value:
        return ""
    return value if len(value) <= MAX_SAMPLE_CELL_CHARS else value[: MAX_SAMPLE_CELL_CHARS - 1] + "…"




HEADER_SYSTEM_PROMPT = """\
You are reading one Request For Quotation sent to a marine ship chandler, and
filling in the top of a fixed form. A shipping company is asking the chandler to
price goods for a named vessel, at a named port, by a named date.

You are NOT reading the list of goods - another reader has it. You want the
facts around it.

When the requisition arrived as a scan or a photograph, its page is attached to
this message. Read the customer's own header block off it - "Enquiry No.",
"Vessel", "Category", "Delivery" - and prefer what is printed there to anything
you would infer from the goods. Cite the file by its name from <sources>.

## Fields

- `vessel_name` - the ship the goods are for.
- `imo` - its IMO number. Seven digits. Never complete a partial one.
- `rfq_reference` - the customer's own reference for this enquiry: an RFQ, req
  or enquiry number. Not our number, not a vessel number.
- `customer_contact` - the person who wrote, by name.
- `customer_phone` - their telephone number.
- `customer_email` - their address. The sender's, unless the text names another.
- `person_designation` - their job title, only if it is written down.
- `delivery_port` - where the goods are wanted.
- `eta` / `etd` - the vessel's arrival and departure.
- `quote_before` - the deadline for our quotation, only when one is stated.
  Never work it out from the ETA or the ETD.
- `requested_delivery` - when the goods are wanted, if that is said separately.
- `delivery_address` - a street address or berth, when one is given.
- `currency` - only if the customer states one.
- `rfq_type` - which department the goods belong to. Exactly one of:
  BOND, CABIN, DECK, ENGINE, PROVISION, ELECTRICAL, MEDICAL, SAFETY,
  STATIONARY, PRIVATE, OTHERS, TENDER.
  Take it from what the customer called the enquiry when they said. When they
  did not, read <goods requested> and answer with the department **most** of
  the items belong to - a list of tinned food and biscuits is PROVISION even
  with a mop in it. Use OTHERS only when it is genuinely mixed - it exists for
  that, and it is better than a wrong department.

## And one thing that is not a field

`customer_company` - the **company** the RFQ came from: the ship manager or
owner, as they write their own name. Not the person, not the vessel, not us.

Take it from the signature block, the letterhead, or the sender's own domain -
whichever spells it out most fully. `purchasing@goodwoodship.com` signed
"Goodwood Ship Management" is `Goodwood Ship Management`, not `goodwoodship.com`.

Leave it out when the email does not say. It is looked up against a list of
2,190 companies, so a shortened or invented name finds the wrong one or nothing;
the full written name is what makes it findable.

## What is NOT there, and why - `not_found`

Six fields are starred on the form and block it when they are empty:
`vessel_name`, `imo`, `rfq_reference`, `delivery_port`, `currency`, `rfq_type`.

For each starred field you are **not** returning in `fields`, add one entry to
`not_found` with one short sentence saying **where you looked and what was
there instead**. This is read by the person who has to fill the cell by hand,
and it saves them searching the whole email again.

  imo         "Not stated anywhere - the vessel is named but never numbered."
  currency    "Neither the email nor the requisition names a currency."
  rfq_type    "The requisition says 'Kategoria: pozostale', which is not one
               of the twelve departments."
  eta         (not starred - do not explain it)

Hard rules, because a wrong explanation is worse than none: it tells the desk
not to bother looking.

- Say only what you actually saw. If you did not find a delivery port, the
  sentence is "no port is named", not "the customer will confirm later".
- Never guess the value in the sentence, and never advise what to do next.
- One sentence, under 140 characters.
- Only the six starred fields, and only the ones you left out of `fields`. An
  entry for a field you did return is a contradiction and is thrown away.

## Rules

Copy the value EXACTLY as written. "12 Oct" stays "12 Oct" - do not turn it into
a date. "Jebel Ali" stays "Jebel Ali" - do not expand it into a port code.
Something else converts these later and needs the original to do it.

Never answer with `sender_code`. It is not written in any email - it is looked
up from `customer_company`, and a code that looks right but belongs to another
customer is the worst mistake available here.

Return ONLY the fields you actually found. A field you leave out is left blank
in the form, which is correct and expected. Never write "N/A", "unknown", "-" or
a guess: a blank cell costs somebody one lookup, a wrong one costs a wrong order.

Every field must name its `source`, using one of the labels listed under
<sources> and nothing else.

If two places give genuinely different values for one field, leave that field
out. A longer and a shorter form of the same name are not different values.

Regex hints under <precomputed> are a machine's guess. Confirm each against the
text before using it, and ignore it when the text disagrees.
"""

HEADER_INSTRUCTION = "Fill in the header fields of this RFQ."

# The customer's own header block sits above the item table - vessel, IMO, port,
# sometimes the reference. Twenty-five rows reaches it in every file seen so far.
MAX_HEADER_ROWS = 25
MAX_HEADER_COLS = 8
MAX_CELL_CHARS = 60
MAX_BODY_CHARS = 6000

EMAIL_SUBJECT = "email.subject"
EMAIL_BODY = "email.body"

# Enough to see which department an RFQ belongs to. Two hundred rows of
# provisions say no more than twenty of them do.
MAX_GOODS_SHOWN = 20
MAX_GOODS_CHARS = 70


def build_header_messages(
    email: NormalizedEmail,
    documents: list["ReadDocument"],
    signals: Signals | None = None,
) -> Messages:
    """The prompt for the header of one RFQ.

    Everything the fields could be hiding in goes in: the email, the top of
    whichever attachment holds the item list, and what the supporting files
    turned out to show. What does not go in is the item list itself - hundreds
    of rows that cannot contain a vessel name.
    """
    sections = [
        f"{HEADER_INSTRUCTION}\n",
        _email_block(email),
        _attachments_block(documents),
    ]
    if goods := _goods_block(documents):
        sections.append(goods)
    if hints := _signals_block(signals):
        sections.append(hints)
    sections.append(_sources_block(documents))

    body = "\n\n".join(sections)
    pictured = _pictured_requisitions(documents)
    if not pictured:
        return [("system", HEADER_SYSTEM_PROMPT), ("human", body)]

    blocks: list[ContentBlock] = [{"type": "text", "text": body}]
    blocks += [image_block(image.data, image.media_type) for image in pictured]
    return [("system", HEADER_SYSTEM_PROMPT), ("human", blocks)]


def _pictured_requisitions(documents: list["ReadDocument"]) -> list[ImageRef]:
    """The picture of the requisition, when a picture is all there is of it.

    A scanned requisition has no text and no grid, so `_attachments_block`
    below can show nothing of it but one sentence - and the customer's own
    header block, the one carrying "Enquiry No.", "Vessel" and "Category", is
    printed on that picture.

    Measured on a real scan: without this the model answered `rfq_type` DECK
    for a requisition whose own category line reads "maintenance/overhaul
    others", and cited the file it could not see.

    Only the files that carry the list, and only the first few pages of them. A
    photograph of a nameplate holds no header block, and an email of four
    product photos would otherwise pay for all four on a call that is made once
    per RFQ whatever else happens.
    """
    return [
        image
        for document in documents
        if document.holds_items and _shows_nothing_but_pictures(document.document)
        for image in document.document.images
    ][:MAX_IMAGES_PER_CALL]


def _shows_nothing_but_pictures(document: Document) -> bool:
    """Nothing `_top_rows` could put in the prompt: no grid, no text, images only."""
    return bool(document.images) and not document.grids and not _prose(document)


def _goods_block(documents: list["ReadDocument"]) -> str:
    """A sample of what is being asked for, and nothing else about it.

    Only so that `rfq_type` can be settled when the customer never named a
    department: "classify line items and use the dominant category", says the
    mapping document. Descriptions only - no quantities, no codes - and a
    sample, because two hundred rows of provisions say no more than twenty.
    """
    described = [
        item.description
        for document in documents
        for item in document.items
        if item.description
    ]
    if not described:
        return ""

    shown = described[:MAX_GOODS_SHOWN]
    lines = [
        f"- {text if len(text) <= MAX_GOODS_CHARS else text[: MAX_GOODS_CHARS - 1] + '…'}"
        for text in shown
    ]
    if len(described) > len(shown):
        lines.append(f"- ... and {len(described) - len(shown)} more")
    return "\n".join(["<goods requested>", *lines, "</goods requested>"])


def _email_block(email: NormalizedEmail) -> str:
    body, _ = truncate(email.body_text or "", MAX_BODY_CHARS)
    return "\n".join(
        [
            "<email>",
            f"from: {_sender(email)}",
            f"received: {email.received_at.isoformat() if email.received_at else '-'}",
            f"subject: {email.subject or '-'}",
            "body:",
            body or "(empty)",
            "</email>",
        ]
    )


def _attachments_block(documents: list["ReadDocument"]) -> str:
    if not documents:
        return "<attachments>\n(none)\n</attachments>"

    parts = ["<attachments>"]
    for reviewed in documents:
        parts.append(f"\n{reviewed.origin} - {reviewed.what or reviewed.document.kind.value}")
        if reviewed.facts:
            parts.append(f"  identifiers visible in it: {', '.join(reviewed.facts)}")
        if reviewed.holds_items:
            parts.append(_top_rows(reviewed))
    parts.append("</attachments>")
    return "\n".join(part for part in parts if part)


def _top_rows(reviewed: "ReadDocument") -> str:
    """The rows above the item table, where the customer's own header block sits.

    It stops at the table's own heading row. Item rows cannot contain a vessel
    name, so every one shown past that point is a row nobody reads.
    """
    lines = []
    for grid in reviewed.document.grids[:1]:
        blocks = find_blocks(grid)
        stop = blocks[0].header_row + 1 if blocks else MAX_HEADER_ROWS
        shown = grid.rows[: min(stop, MAX_HEADER_ROWS)]
        lines.append(f"  first {len(shown)} row(s):")
        lines += [
            "    " + " | ".join(_cell(cell) for cell in row[:MAX_HEADER_COLS])
            for row in shown
        ]
    if not lines and (text := _prose(reviewed.document)):
        lines.append(f"  text:\n{text}")
    return "\n".join(lines)


def _signals_block(signals: Signals | None) -> str:
    if signals is None:
        return ""
    found = {
        name: value
        for name, value in signals.model_dump().items()
        if value and name != "urgency_markers"
    }
    if not found:
        return ""

    lines = [f"  {name}: {value}" for name, value in found.items()]
    return "<precomputed>\nRegex guesses, to be confirmed against the text:\n" + "\n".join(lines) + "\n</precomputed>"


def _sources_block(documents: list["ReadDocument"]) -> str:
    labels = [EMAIL_SUBJECT, EMAIL_BODY, *(reviewed.origin for reviewed in documents)]
    return (
        "<sources>\nUse exactly one of these as `source`:\n"
        + "\n".join(f"  {label}" for label in labels)
        + "\n</sources>"
    )


def _prose(document: Document) -> str:
    """The file's words, from both ends rather than the front.

    Front-truncation is the wrong cut for an RFQ form: they open with pages of
    compliance boilerplate - Hong Kong Convention, EU SRR, asbestos - and the
    items are underneath it.
    """
    text = (document.text or "\n\n".join(page.text for page in document.pages)).strip()
    trimmed, _ = truncate(text, MAX_BODY_CHARS)
    return trimmed


def _cell(value: str | None) -> str:
    if not value:
        return ""
    return value if len(value) <= MAX_CELL_CHARS else value[: MAX_CELL_CHARS - 1] + "…"


def _sender(email: NormalizedEmail) -> str:
    if not email.sender:
        return "-"
    name, address = email.sender.name, email.sender.address
    return f"{name} <{address}>" if name and address else (name or address or "-")


def source_labels(documents: list["ReadDocument"]) -> set[str]:
    """The labels the model was told to cite. Used to check that it did."""
    return {EMAIL_SUBJECT, EMAIL_BODY, *(reviewed.origin for reviewed in documents)}


ITEMS_SYSTEM_PROMPT = """\
You are reading the list of goods a shipping company wants a marine ship chandler
to price. It is not in a table this time - it is written out in a document, on a
scan, or in a photograph - so you have to transcribe it.

## What is an item

A thing the customer wants quoted, with however much detail they gave. Most of
these lists carry no item code at all: a description alone is a complete item.

## Rules

Transcribe EVERY item, in the order they appear. Do not merge two into one and
do not split one into two.

Copy each value EXACTLY as written. "2 coils" stays "2 coils". "1,5" stays "1,5".
Do not convert, expand abbreviations, correct spelling or translate.

Leave a field out rather than inventing it. No item code is normal and correct.

`description` carries the technical detail that belongs to the article - size,
material, rating, standard. It does not carry the price, the delivery terms or
a remark about the whole order.

Ignore everything that is not an item: letterheads, addresses, prices, totals,
payment terms, compliance notices, signatures, page numbers.

Give each item the `page` label it came from, from the list in <parts>.

If <already_read> is present, those items were transcribed in an earlier part.
Do not repeat them; continue from where they stop.
"""

ITEMS_INSTRUCTION = "Transcribe the items in <parts>."

# Text per call. Small enough that accuracy holds, big enough that an ordinary
# requisition is one or two calls.
MAX_CHUNK_CHARS = 4000
# Images per call. A vision model reading four scanned pages at once starts
# blending their rows together.
MAX_IMAGES_PER_CALL = 3
# The tail of the previous part, so an item split across a page boundary is
# recognisable as one already seen.
OVERLAP_CHARS = 400


def build_items_messages(
    texts: list[tuple[str, str]],
    images: list[ImageRef],
    already_read: str = "",
) -> Messages:
    """The prompt for one part of a document that has no table in it."""
    sections = [f"{ITEMS_INSTRUCTION}\n"]
    if already_read:
        sections.append(
            "<already_read>\nThe end of the previous part, already transcribed. "
            f"Do not repeat it.\n{already_read}\n</already_read>"
        )

    sections.append("<parts>")
    for label, text in texts:
        sections.append(f"\n[{label}]\n{text}")
    for image in images:
        sections.append(f"\n[{image.origin}] - attached below as an image")
    sections.append("</parts>")

    text_part = "\n".join(sections)
    if not images:
        return [("system", ITEMS_SYSTEM_PROMPT), ("human", text_part)]

    blocks: list[ContentBlock] = [{"type": "text", "text": text_part}]
    blocks += [image_block(image.data, image.media_type) for image in images]
    return [("system", ITEMS_SYSTEM_PROMPT), ("human", blocks)]
