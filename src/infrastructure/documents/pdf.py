"""PDFs, which arrive as two completely different things under one extension.

A PDF exported from a procurement system carries a text layer and is read for
free. A PDF that is a photocopy carries no text at all and has to be rasterized
and shown to a vision model. The declared content type is identical for both, so
the only way to tell them apart is to pull the text out and count it.

Three libraries, each for one job: pypdf for the fast text pass and the
encryption check, pdfplumber for table geometry, pypdfium2 for rendering. All
three are permissively licensed. PyMuPDF would do all of it in one, but it is
AGPL-3.0 unless bought, which is not a dependency to acquire by accident.
"""

import io
import logging

import pdfplumber
import pypdf
import pypdfium2

from src.infrastructure.documents.budget import Budget
from src.infrastructure.documents.image import encode_image
from src.infrastructure.documents.models import (
    PAGES_TRUNCATED,
    PASSWORD_PROTECTED,
    SCANNED_PDF,
    UNREADABLE,
    Document,
    Grid,
    Page,
)

logger = logging.getLogger(__name__)

# pypdf warns once per embedded font that fontTools would parse it better. We do
# not act on it and it drowns everything else when a folder is inspected.
logging.getLogger("pypdf._cmap").setLevel(logging.ERROR)

# Under this many characters per page, there is no usable text layer. A text PDF
# runs into the thousands; a scan yields the odd stray glyph from a stamp.
MIN_CHARS_PER_PAGE = 50

# A page carrying an embedded image and little else is a picture with a caption,
# and the picture is the content. A presentation of email screenshots exported
# to PDF measured 119 characters a page across eighteen image-backed pages -
# comfortably over the threshold above, and every screenshot would have been
# thrown away for the sake of the slide titles.
MIN_CHARS_PER_IMAGE_PAGE = 400
MIN_IMAGE_PAGE_SHARE = 0.5

# 2x is roughly 144 DPI - enough for a vision model to read a printed table
# without pushing every page over a megabyte.
RENDER_SCALE = 2.0


def read_pdf(document: Document, data: bytes, budget: Budget) -> Document:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as error:  # noqa: BLE001 - pypdf raises several unrelated types
        document.warn(UNREADABLE)
        logger.debug("pypdf could not open %s: %s", document.filename, error)
        return document

    if reader.is_encrypted:
        # An empty user password is common and openable; a real one is not.
        try:
            opened = reader.decrypt("")
        except Exception:  # noqa: BLE001
            opened = 0
        if not opened:
            document.warn(PASSWORD_PROTECTED)
            return document

    total = len(reader.pages)
    limit = min(total, budget.max_pdf_pages_text)
    if total > limit:
        document.warn(PAGES_TRUNCATED)
        document.truncated = True

    texts = [_page_text(reader, index) for index in range(limit)]

    if limit and _is_pictures(reader, texts, limit):
        document.warn(SCANNED_PDF)
        return _rasterize(document, data, budget, total)

    document.pages = [
        Page(origin=f"{document.origin}#p{index + 1}", number=index + 1, text=text)
        for index, text in enumerate(texts)
    ]
    _add_tables(document, data, budget, limit)
    document.text = "\n\n".join(page.text for page in document.pages if page.text)
    return document


def _is_pictures(reader: pypdf.PdfReader, texts: list[str], pages: int) -> bool:
    """Is the content in the pictures rather than in the text layer?

    Two ways to be: almost no text at all, or a little text on pages that are
    mostly image. Counting image XObjects reads the page dictionary and decodes
    nothing, so it costs milliseconds even on a six-megabyte file.
    """
    per_page = sum(len(text) for text in texts) / pages
    if per_page < MIN_CHARS_PER_PAGE:
        return True

    image_backed = sum(1 for index in range(pages) if _has_image(reader.pages[index]))
    return (
        image_backed / pages >= MIN_IMAGE_PAGE_SHARE
        and per_page < MIN_CHARS_PER_IMAGE_PAGE
    )


def _has_image(page: pypdf.PageObject) -> bool:
    try:
        objects = page.get("/Resources", {}).get("/XObject", {})
        return any(objects[name].get("/Subtype") == "/Image" for name in objects)
    except Exception:  # noqa: BLE001 - a malformed resource dict is not an image
        return False


def _page_text(reader: pypdf.PdfReader, index: int) -> str:
    try:
        return (reader.pages[index].extract_text() or "").strip()
    except Exception as error:  # noqa: BLE001 - one broken page must not lose the file
        logger.debug("Page %d unreadable: %s", index + 1, error)
        return ""


def _add_tables(document: Document, data: bytes, budget: Budget, pages: int) -> None:
    """Recover table geometry, which the text pass flattens into prose.

    pdfplumber is an order of magnitude slower than the text pass, so only the
    front of the document is mined: an RFQ's item list is not on page 60.
    """
    limit = min(pages, budget.max_pdf_pages_with_tables)
    by_number = {page.number: page for page in document.pages}

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for index in range(limit):
                for table in pdf.pages[index].extract_tables() or []:
                    rows = [[_text_or_none(cell) for cell in row] for row in table]
                    if any(cell for row in rows for cell in row):
                        by_number[index + 1].tables.append(
                            Grid(origin=f"{document.origin}#p{index + 1}", rows=rows)
                        )
    except Exception as error:  # noqa: BLE001 - tables are a bonus, text is the floor
        logger.debug("pdfplumber found no tables in %s: %s", document.filename, error)


def _rasterize(
    document: Document, data: bytes, budget: Budget, total_pages: int
) -> Document:
    """Turn a scan into images a vision model can read."""
    limit = min(total_pages, budget.max_pdf_pages_rasterized)
    if total_pages > limit:
        document.warn(PAGES_TRUNCATED)
        document.truncated = True

    try:
        pdf = pypdfium2.PdfDocument(data)
    except Exception as error:  # noqa: BLE001
        document.warn(UNREADABLE)
        logger.debug("pypdfium2 could not render %s: %s", document.filename, error)
        return document

    try:
        for index in range(limit):
            pixels = pdf[index].render(scale=RENDER_SCALE).to_pil()
            document.images.append(
                encode_image(pixels, origin=f"{document.origin}#p{index + 1}", budget=budget)
            )
    finally:
        pdf.close()

    return document


def _text_or_none(cell: str | None) -> str | None:
    return cell.strip() or None if cell else None
