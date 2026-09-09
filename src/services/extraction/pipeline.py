"""One RFQ email in, one populated extraction out.

Read every attachment (`read`), then read the header and map everything onto
what the workbook accepts (`run`). Two steps rather than one because the triage
verdict sits between them: it has to be shown what the files hold before it can
say whether this is an RFQ at all. Stage C takes it from here and never learns
that a model was involved.

Three things happen here that no single reader could do, because each of them
needs to see the whole email at once: the body is read as an attachment when no
real one carried the list, two files holding the same requisition are read once,
and the header sees every file together.

An ordinary RFQ - one covering email, one spreadsheet - costs three model calls:
triage, the file, the header.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

from src.domain.models import NormalizedEmail, Signals
from src.infrastructure.documents.models import Document, FileKind
from src.services.extraction.file_reader import FileReader
from src.services.extraction.header import HeaderReader
from src.services.extraction.models import (
    DUPLICATE_ITEM_SOURCE,
    NO_ITEMS_ANYWHERE,
    REQUIRED_HEADER_FIELDS,
    SAME_ITEM_TWO_QUANTITIES,
    HeaderField,
    LineItem,
    NormalizedHeader,
    ReadDocument,
    RfqHeader,
)
from src.services.extraction.normalize import normalize_header

logger = logging.getLogger(__name__)

EMAIL_BODY = "email.body"

# Enough of a description to recognise the row on the form, no more: the
# conflict line names two files as well and has to stay one line.
MAX_ARTICLE_SHOWN = 48

# The starred cells, in the order the template lists them - which is the order a
# person reads them off the form.
REQUIRED_HEADER_FIELDS_IN_ORDER = [
    name for name in HeaderField if name in REQUIRED_HEADER_FIELDS
]


@dataclass(frozen=True, slots=True)
class RfqExtraction:
    """Everything read out of one RFQ, ready for the workbook."""

    header: RfqHeader
    normalized: NormalizedHeader
    items: list[LineItem] = field(default_factory=list)
    documents: list[ReadDocument] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Columns the customer filled in that no template field takes, e.g.
    # "F (REMARKS)". Reported to a person rather than dropped in silence.
    unmapped_columns: list[str] = field(default_factory=list)
    # Two attachments giving the same article different quantities, already
    # worded for a person. Not resolved here - see `_merge`.
    conflicts: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """Every starred field filled and at least one item. What decides
        whether the forward goes out plain or with a caveat attached."""
        return bool(self.items) and not self.missing_required

    def journal_payload(self) -> dict[str, object]:
        """What the decision log keeps about this extraction.

        Shaped here rather than in the journal, which must not learn what an
        extraction is. Every judgement and the evidence behind it goes in,
        because the question this line answers is "why is this cell blank?" -
        and from the finished workbook that is unanswerable: the blank looks
        the same whether nobody wrote the value, the model missed it, or the
        port was real but not in this branch's dropdown.
        """
        return {
            "documents": [_journalled(item) for item in self.documents],
            "header": self._journalled_header(),
            # Separate from `header`, because a field can be refused and filled
            # in the same answer: the model claiming one twice keeps the first.
            "header_refused": [
                {"field": item.field.value, "value": item.value, "why": item.why}
                for item in self.header.refused
            ],
            "items": {
                "count": len(self.items),
                "sources": sorted({item.source for item in self.items}),
                "unmapped_columns": self.unmapped_columns,
                "conflicts": self.conflicts,
            },
            "complete": self.is_complete,
            "missing_required": self.missing_required,
            "warnings": self.warnings,
        }

    def _journalled_header(self) -> dict[str, dict[str, object]]:
        """Every field as written, as mapped, or as lost - in one place.

        A value the customer wrote and B6 could not map shows its original
        beside the reason, which is the pair that explains an empty cell.
        """
        fields: dict[str, dict[str, object]] = {
            name.value: {"raw": found.value, "source": found.source}
            for name, found in self.header.fields.items()
        }
        for name, value in self.normalized.text.items():
            fields.setdefault(name.value, {})["value"] = value
        for name, day in self.normalized.dates.items():
            fields.setdefault(name.value, {})["value"] = day.isoformat()
        for name, reason in self.normalized.dropped.items():
            fields.setdefault(name.value, {})["dropped"] = reason
        return fields

    @property
    def missing_required(self) -> list[str]:
        """Starred cells that will go out blank, whatever the reason.

        One rule rather than two: "the model never found it" and "the model
        found `Dubai` and five ports match it" leave the same empty cell, and
        the person checking the forward cares about the cell.
        """
        return [
            name.value
            for name in REQUIRED_HEADER_FIELDS_IN_ORDER
            if not self.normalized.has(name)
        ]


class ExtractionPipeline:
    """B2 to B6, in order."""

    def __init__(self, files: FileReader, header: HeaderReader) -> None:
        self._files = files
        self._header = header

    async def read(self, documents: list[Document]) -> list[ReadDocument]:
        """Read every attachment, once. One model call per file, all at once.

        Split out of `run` because the triage verdict comes between the two: an
        email whose body says no more than "please find attached" is an RFQ
        because of what is in the file, so the classifier has to be shown these
        answers before it decides. The same list then goes straight into `run`,
        which is what stops the files being read a second time.
        """
        return list(await asyncio.gather(*(self._files.read(item) for item in documents)))

    async def run(
        self,
        email: NormalizedEmail,
        documents: list[ReadDocument],
        *,
        signals: Signals | None = None,
        today: date | None = None,
    ) -> RfqExtraction:
        """Read one RFQ out of files that have already been read individually.

        Never raises: a failed step becomes a warning. `documents` comes from
        `read` above, and is left as the caller passed it - the classifier holds
        the same list, and the body this may add is not one of its attachments.
        """
        read = list(documents)

        if not any(item.items for item in read):
            # Customers type the list straight into the message often enough
            # that it has to be covered - but only when no attachment produced
            # a single row, which saves a call on every ordinary RFQ.
            #
            # The test is "nothing came out", not "no file claims to hold the
            # list". A file that was called a requisition and yielded nothing
            # used to silence this last chance, which is the one case where it
            # is most needed: whatever went wrong with the attachment, the body
            # may still carry the list.
            read.append(await self._files.read(body_document(email)))

        merged = _merge(read)
        header = await self._header.read(email, read, signals)

        normalized = normalize_header(header, today=today)

        warnings = list(merged.warnings)
        warnings += [code for code in header.warnings if code not in warnings]
        warnings += [code for code in normalized.warnings if code not in warnings]
        warnings += [
            code
            for document in read
            for code in document.warnings + document.document.warnings
            if code not in warnings
        ]

        # The one line that says what this RFQ came out as. Every other stage
        # logs its own step; a reader following an email wants this one, so it
        # carries the warnings too - they are the reason a cell is empty.
        logger.info(
            "Extracted %d item(s), %d header field(s) | missing %s | warnings %s",
            len(merged.items),
            len(normalized.text) + len(normalized.dates),
            [name.value for name in REQUIRED_HEADER_FIELDS_IN_ORDER if not normalized.has(name)]
            or "nothing starred",
            warnings or "none",
        )
        return RfqExtraction(
            header=header,
            normalized=normalized,
            items=merged.items,
            documents=read,
            warnings=warnings,
            unmapped_columns=merged.unmapped_columns,
            conflicts=merged.conflicts,
        )


@dataclass(frozen=True, slots=True)
class _Merged:
    """Every source's items in one list, plus what did not add up."""

    items: list[LineItem]
    warnings: list[str]
    unmapped_columns: list[str]
    conflicts: list[str]


def _merge(read: list[ReadDocument]) -> _Merged:
    """Every source's items in one numbered list, each row kept once.

    The same requisition arrives twice often enough to matter - as a PDF and as
    the spreadsheet it was printed from - and reading both would double every
    line.

    Two versions of one list are the harder case, and this does **not** resolve
    it. An RFQ that arrives with the previous revision attached "for reference"
    gives the same article twice with different quantities, and nothing in the
    files says which one stands - only the covering sentence does. Both rows go
    in and the disagreement is reported: a doubled order is expensive, and so
    is quietly dropping the quantity the customer actually meant.
    """
    items: list[LineItem] = []
    warnings: list[str] = []
    unmapped: list[str] = []
    conflicts: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    # Article -> the first quantity written for it, and the file that wrote it.
    quantities: dict[tuple[str, str], tuple[str, str]] = {}

    for source in read:
        warnings += [code for code in source.warnings if code not in warnings]
        unmapped += [column for column in source.unmapped_columns if column not in unmapped]

        fresh = [item for item in source.items if _identity(item) not in seen]
        if source.items and not fresh:
            logger.info("%s repeats items already read, skipping", source.origin)
            if DUPLICATE_ITEM_SOURCE not in warnings:
                warnings.append(DUPLICATE_ITEM_SOURCE)
            continue

        for item in fresh:
            seen.add(_identity(item))
            if conflict := _quantity_conflict(item, quantities):
                conflicts.append(conflict)
            items.append(_renumbered(item, len(items) + 1))

    if not items and NO_ITEMS_ANYWHERE not in warnings:
        warnings.append(NO_ITEMS_ANYWHERE)
    if conflicts and SAME_ITEM_TWO_QUANTITIES not in warnings:
        warnings.append(SAME_ITEM_TWO_QUANTITIES)
    return _Merged(items, warnings, unmapped, conflicts)


def _quantity_conflict(
    item: LineItem, quantities: dict[tuple[str, str], tuple[str, str]]
) -> str:
    """The same article, a different quantity, and a different file. Or nothing.

    Only across files. Inside one requisition a repeated article at another
    quantity is the customer's own row - a split delivery, two departments -
    and code has no business calling that a mistake. Measured on both finished
    RFQs the desk sent us: 222 rows, not one article repeated. So two files
    disagreeing is a revision, not a list.
    """
    article = (_plain(item.customer_item_code), _plain(item.description))
    file = _file_of(item)
    written, where = quantities.get(article, ("", ""))

    if not where:
        quantities[article] = (_plain(item.quantity), file)
        return ""
    if where == file or written == _plain(item.quantity):
        return ""

    shown = (item.description or "")[:MAX_ARTICLE_SHOWN]
    return f'{shown}: {item.quantity} in "{file}", {written} in "{where}"'


def _file_of(item: LineItem) -> str:
    """The file a row came out of, without the sheet and row after it."""
    return item.source.split("#")[0]


def _plain(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def _journalled(read: ReadDocument) -> dict[str, object]:
    """One attachment: what arrived, what came out of it, what it was taken for.

    The counts matter as much as the role. "No items found" reads very
    differently against `grids: 1` than against `grids: 0, images: 0`, and only
    one of those is a parser problem.
    """
    document = read.document
    return {
        "origin": read.origin,
        "kind": document.kind.value,
        "size_bytes": document.size_bytes,
        "declared_content_type": document.declared_content_type,
        "read": {
            "grids": len(document.grids),
            "pages": len(document.pages),
            "images": len(document.images),
            "text_chars": len(document.text),
            "truncated": document.truncated,
        },
        "role": read.role.value,
        "what": read.what,
        "facts": read.facts,
        "items": len(read.items),
        "warnings": document.warnings + read.warnings,
    }


def body_document(email: NormalizedEmail) -> Document:
    """The email itself, as an attachment.

    Customers type the list straight into the message often enough that a
    separate path for it would be a second implementation of B2, B3 and B5.
    Making it a `Document` means the body is reviewed, routed and read by the
    same code as everything else, and cites itself the same way.
    """
    return Document(
        filename=EMAIL_BODY,
        kind=FileKind.TEXT,
        size_bytes=len(email.body_text or ""),
        text=email.body_text or "",
    )


def _identity(item: LineItem) -> tuple[str, str, str]:
    """What makes two rows the same row, across files as well as within one."""
    return tuple(
        " ".join((value or "").lower().split())
        for value in (item.customer_item_code, item.description, item.quantity)
    )  # ty: ignore


def _renumbered(item: LineItem, number: int) -> LineItem:
    """Sr. No runs 1..N over the whole RFQ, not per file."""
    return LineItem(
        sr_no=number,
        description=item.description,
        customer_item_code=item.customer_item_code,
        quantity=item.quantity,
        uom=item.uom,
        source=item.source,
    )
