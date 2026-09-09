"""One email's attachments become a list of `Document`s.

This is the only place that knows the whole set of readers, and the only place
that recurses: an archive or an attached email hands its contents straight back
into `_read`, one level down and no further.

Nothing here raises on bad input. A reader that fails returns a Document
carrying a warning code, because the alternative - one unreadable drawing
aborting the email - loses the RFQ sitting beside it.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from src.infrastructure.documents import (
    archive,
    markup,
    nested_email,
    office,
    pdf,
    spreadsheet,
)
from src.infrastructure.documents.budget import Budget, BudgetTracker, truncate
from src.infrastructure.documents.detect import sniff
from src.infrastructure.documents.image import read_image
from src.infrastructure.documents.models import (
    ATTACHMENT_HAS_NO_BYTES,
    ATTACHMENT_IS_A_LINK,
    IMAGES_TRUNCATED,
    NESTED_TOO_DEEP,
    NO_READER_FOR_KIND,
    TEXT_TRUNCATED,
    Document,
    FileKind,
)

logger = logging.getLogger(__name__)


def _fit_images(documents: list[Document], budget: Budget) -> None:
    """Share the email's image budget out, rather than spending it in file order.

    Measured on the sample set: an eighteen-page presentation and three photos
    of drill bits. First come, first served gave the presentation all ten slots
    and the photos none - and the photos were the ones carrying part numbers.
    """
    claimants = [document for document in documents if document.images]
    if sum(len(document.images) for document in claimants) <= budget.max_images:
        return

    for document, allowed in zip(
        claimants,
        _allocate([len(document.images) for document in claimants], budget.max_images),
        strict=True,
    ):
        if len(document.images) > allowed:
            document.images = document.images[:allowed]
            document.warn(IMAGES_TRUNCATED)
            document.truncated = True


def _allocate(wanted: list[int], total: int) -> list[int]:
    """An equal share each, then the remainder to whoever still wants more.

    A lone scan is unaffected: with one claimant the first round hands it
    everything. When there are more claimants than slots nobody can be
    guaranteed one, and the files are taken in the order they arrived.
    """
    share = total // len(wanted)
    if share == 0:
        return [1 if index < total else 0 for index in range(len(wanted))]

    given = [min(count, share) for count in wanted]
    spare = total - sum(given)

    for index, count in enumerate(wanted):
        extra = min(spare, count - given[index])
        given[index] += extra
        spare -= extra
    return given


# kind -> the function that turns bytes into content. Kinds absent from this map
# are recognised but not readable, and say so rather than failing quietly.
_READERS = {
    FileKind.XLSX: spreadsheet.read_xlsx,
    FileKind.XLS: spreadsheet.read_xls,
    FileKind.CSV: spreadsheet.read_csv,
    FileKind.TEXT: spreadsheet.read_text,
    FileKind.PDF: pdf.read_pdf,
    FileKind.DOCX: office.read_docx,
    FileKind.PPTX: office.read_pptx,
    FileKind.IMAGE: read_image,
    FileKind.HTML: markup.read_html,
}

_ARCHIVES = (FileKind.ZIP, FileKind.SEVEN_ZIP)
_EMAILS = (FileKind.EML, FileKind.MSG)


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One attachment as it left the mailbox, before anything has looked inside.

    `data` is None for a reference attachment - a OneDrive or SharePoint link
    that carries a URL and no bytes at all.
    """

    filename: str
    data: bytes | None
    content_type: str | None = None
    size_bytes: int = 0
    is_inline: bool = False
    is_reference: bool = False


class DocumentLoader:
    """Reads one email's attachments within one budget."""

    def __init__(self, budget: Budget | None = None) -> None:
        self._budget = budget or Budget()

    def load(self, files: Iterable[SourceFile]) -> list[Document]:
        """Every attachment, in the order it arrived, dropped ones included.

        A dropped file still appears, carrying the code that dropped it. The
        reviewer has to be able to see that attachment 21 existed.
        """
        tracker = BudgetTracker(self._budget)
        documents: list[Document] = []

        for source in files:
            if self._is_signature_image(source):
                logger.debug("Skipping inline image %s", source.filename)
                continue
            documents.extend(self._read(source, tracker, depth=0, parent=None))

        _fit_images(documents, self._budget)
        return documents

    def _read(
        self,
        source: SourceFile,
        tracker: BudgetTracker,
        *,
        depth: int,
        parent: str | None,
    ) -> list[Document]:
        document = Document(
            filename=source.filename,
            kind=FileKind.UNKNOWN,
            size_bytes=source.size_bytes or len(source.data or b""),
            declared_content_type=source.content_type,
            parent=parent,
        )

        if source.is_reference:
            # A link, not a file. Fetching it needs Files.Read permissions this
            # app does not hold, so it is a person's job.
            document.warn(ATTACHMENT_IS_A_LINK)
            return [document]

        if not source.data:
            document.warn(ATTACHMENT_HAS_NO_BYTES)
            return [document]

        blocked = tracker.may_open(document.size_bytes)
        if blocked:
            document.warn(blocked)
            return [document]

        document.kind = sniff(source.data, source.filename, source.content_type)

        if document.kind in _ARCHIVES:
            return [document, *self._expand_archive(document, source.data, tracker, depth)]
        if document.kind in _EMAILS:
            return [document, *self._expand_email(document, source.data, tracker, depth)]

        reader = _READERS.get(document.kind)
        if reader is None:
            document.warn(NO_READER_FOR_KIND)
            return [document]

        reader(document, source.data, self._budget)
        self._apply_caps(document, tracker)
        return [document]

    def _expand_archive(
        self, document: Document, data: bytes, tracker: BudgetTracker, depth: int
    ) -> list[Document]:
        if depth >= self._budget.max_archive_depth:
            document.warn(NESTED_TOO_DEEP)
            return []

        entries, warnings = archive.unpack(data, document.kind is FileKind.ZIP, self._budget)
        for code in warnings:
            document.warn(code)

        return self._children(entries, document, tracker, depth)

    def _expand_email(
        self, document: Document, data: bytes, tracker: BudgetTracker, depth: int
    ) -> list[Document]:
        unpack = nested_email.unpack_eml if document.kind is FileKind.EML else nested_email.unpack_msg
        body, entries, warnings = unpack(data)

        document.text, cut = truncate(body, self._budget.max_chars_per_document)
        if cut:
            document.warn(TEXT_TRUNCATED)
            document.truncated = True
        for code in warnings:
            document.warn(code)

        if depth >= self._budget.max_archive_depth:
            if entries:
                document.warn(NESTED_TOO_DEEP)
            return []

        return self._children(entries, document, tracker, depth)

    def _children(
        self,
        entries: Sequence[tuple[str, bytes]],
        document: Document,
        tracker: BudgetTracker,
        depth: int,
    ) -> list[Document]:
        children: list[Document] = []
        for name, payload in entries:
            child = SourceFile(filename=name, data=payload, size_bytes=len(payload))
            children.extend(
                self._read(child, tracker, depth=depth + 1, parent=document.origin)
            )
        return children

    def _apply_caps(self, document: Document, tracker: BudgetTracker) -> None:
        """Text length, which is per document. Images are shared and come later."""
        document.text, cut = truncate(document.text, self._budget.max_chars_per_document)
        if cut:
            document.warn(TEXT_TRUNCATED)
            document.truncated = True

    def _is_signature_image(self, source: SourceFile) -> bool:
        """A small inline image is a logo under someone's name, not an RFQ."""
        size = source.size_bytes or len(source.data or b"")
        return source.is_inline and size <= self._budget.inline_noise_bytes
