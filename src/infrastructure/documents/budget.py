"""How much of one email we are willing to read.

Microsoft publishes no limit on the number of attachments; the binding platform
constraint is the total message size. Everything here is therefore ours, chosen
for cost and latency.

The rule that matters more than any number: **a breach is never silent.** It
raises a warning code and the RFQ goes to a person. Quietly ignoring attachment
twenty-one is its own way of inventing "there were only twenty", and the task
says not to invent.
"""

from dataclasses import dataclass

from src.infrastructure.documents.models import (
    FILE_TOO_LARGE,
    TOO_MANY_ATTACHMENTS,
    TOTAL_SIZE_EXCEEDED,
)

_MB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Budget:
    """Ceilings for one email. Every field is a setting, none is a platform limit."""

    max_attachments: int = 20
    max_total_bytes: int = 40 * _MB
    max_single_bytes: int = 25 * _MB

    # Signature logos: inline, tiny, and never part of an RFQ.
    inline_noise_bytes: int = 15 * 1024

    max_pdf_pages_text: int = 100
    # Rasterizing is the expensive path, and a drawing on page 40 of a scan is
    # not what an RFQ's item list looks like.
    max_pdf_pages_rasterized: int = 20
    # Tables cost far more per page than text; only the front of a PDF is mined.
    max_pdf_pages_with_tables: int = 20

    # The template's own ceiling is F24:F5000.
    max_spreadsheet_rows: int = 5000
    max_spreadsheet_cols: int = 64

    max_images: int = 10
    max_image_edge: int = 1568

    max_archive_depth: int = 1
    max_archive_entries: int = 50
    max_archive_uncompressed: int = 200 * _MB

    max_chars_per_document: int = 60_000


@dataclass(slots=True)
class BudgetTracker:
    """The running totals for one email.

    Per-email rather than per-file: three 15 MB drawings are individually fine
    and collectively not.
    """

    budget: Budget
    attachments_seen: int = 0
    bytes_seen: int = 0
    images_kept: int = 0

    def may_open(self, size_bytes: int) -> str | None:
        """Reserve room for one file. Returns the warning code that blocked it, or None."""
        if self.attachments_seen >= self.budget.max_attachments:
            return TOO_MANY_ATTACHMENTS
        if size_bytes > self.budget.max_single_bytes:
            # Counted as seen: it arrived, we just will not read it.
            self.attachments_seen += 1
            return FILE_TOO_LARGE
        if self.bytes_seen + size_bytes > self.budget.max_total_bytes:
            self.attachments_seen += 1
            return TOTAL_SIZE_EXCEEDED

        self.attachments_seen += 1
        self.bytes_seen += size_bytes
        return None

    def images_left(self) -> int:
        """How many more images may still go to a vision model."""
        return max(0, self.budget.max_images - self.images_kept)

    def took_images(self, count: int) -> None:
        self.images_kept += count


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head and the tail, and say in the middle what was dropped.

    Both ends matter: an RFQ's header sits at the top and its delivery terms at
    the bottom, so cutting only the tail loses half the fields we came for.
    """
    if len(text) <= limit:
        return text, False

    keep = (limit - 64) // 2
    dropped = len(text) - keep * 2
    return f"{text[:keep]}\n\n[... {dropped} characters omitted ...]\n\n{text[-keep:]}", True
