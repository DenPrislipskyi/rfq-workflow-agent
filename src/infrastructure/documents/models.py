"""What a parsed attachment looks like, whatever it arrived as.

Every reader in this package produces one `Document`. The extraction stage that
follows reads only this shape, so it never has to know whether a table came out
of an Excel sheet, a PDF or a scan.

Every piece carries an `origin` - "Requisition.xlsx#Sheet1", "spec.pdf#p3". That
string is what an extracted field later cites as its source, and a value that
cannot cite one is a bug rather than an answer. It is the whole reason "do not
invent missing or ambiguous values" is checkable instead of merely requested.

These are dataclasses, not pydantic models: nothing here crosses an HTTP
boundary, and `ImageRef.data` is megabytes that must not be copied through a
validator.
"""

from dataclasses import dataclass, field
from enum import StrEnum

# --- Warning codes --------------------------------------------------------
# One place for all of them, because the review policy downstream has to map
# every code to "can this still be processed automatically?" and a typo in a
# free-form string would silently answer yes.

ATTACHMENT_IS_A_LINK = "attachment_is_a_link"
ATTACHMENT_HAS_NO_BYTES = "attachment_has_no_bytes"
TOO_MANY_ATTACHMENTS = "too_many_attachments"
TOTAL_SIZE_EXCEEDED = "total_attachment_size_exceeded"
FILE_TOO_LARGE = "file_too_large"
NO_READER_FOR_KIND = "no_reader_for_kind"
UNREADABLE = "unreadable"
PASSWORD_PROTECTED = "password_protected"
ROWS_TRUNCATED = "rows_truncated"
PAGES_TRUNCATED = "pages_truncated"
TEXT_TRUNCATED = "text_truncated"
IMAGES_TRUNCATED = "images_truncated"
ARCHIVE_ENTRIES_TRUNCATED = "archive_entries_truncated"
ARCHIVE_TOO_BIG = "archive_too_big"
NESTED_TOO_DEEP = "nested_too_deep"
FORMULA_VALUES_MISSING = "formula_values_missing"
SCANNED_PDF = "scanned_pdf"


class FileKind(StrEnum):
    """What the bytes say the file is. Decided by `detect.sniff`, never trusted
    from the filename."""

    XLSX = "XLSX"  # .xlsx and .xlsm - the same reader handles both
    XLS = "XLS"
    CSV = "CSV"
    PDF = "PDF"
    DOCX = "DOCX"
    DOC = "DOC"  # legacy binary Word: detected, but there is no reader
    PPTX = "PPTX"
    IMAGE = "IMAGE"
    TEXT = "TEXT"
    HTML = "HTML"
    EML = "EML"
    MSG = "MSG"
    ZIP = "ZIP"
    SEVEN_ZIP = "SEVEN_ZIP"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Grid:
    """A rectangle of cells: one worksheet, one CSV, or one table lifted off a page.

    Cells are strings because that is what the customer typed. Turning "1,5" or
    "12 pcs" into a number is a decision for the normalization stage, which
    knows the field it is filling; a reader that guesses here loses the original.
    """

    origin: str
    rows: list[list[str | None]] = field(default_factory=list)
    name: str | None = None

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def is_empty(self) -> bool:
        return not any(cell for row in self.rows for cell in row)


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a paged document."""

    origin: str
    number: int
    text: str = ""
    tables: list[Grid] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ImageRef:
    """An image ready to hand to a vision model: rotated, downscaled, re-encoded."""

    origin: str
    media_type: str
    data: bytes = field(repr=False)
    width: int = 0
    height: int = 0

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(slots=True)
class Document:
    """One attachment after it has been read.

    A reader that fails does not raise: it returns a Document carrying the
    warning. One unreadable drawing must not cost us the RFQ attached beside it.
    """

    filename: str
    kind: FileKind
    size_bytes: int = 0
    declared_content_type: str | None = None
    text: str = ""
    grids: list[Grid] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    # Set when the file came out of an archive or an attached email.
    parent: str | None = None

    @property
    def origin(self) -> str:
        """How this file is cited in a source locator."""
        return f"{self.parent} > {self.filename}" if self.parent else self.filename

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.grids or self.pages or self.images)

    def warn(self, code: str) -> None:
        """Record a code once. The same limit hit twice is still one fact."""
        if code not in self.warnings:
            self.warnings.append(code)
