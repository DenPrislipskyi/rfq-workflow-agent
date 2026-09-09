"""Cutting a document into parts small enough to transcribe accurately.

Only for the files with no table in them - prose in a PDF, a list typed into
the email, a photographed requisition. A file whose items sit in a grid is
never cut: code copies its cells whole.

No format is named here. Stage A already reduced every attachment to text,
pages and images.
"""

from dataclasses import dataclass, field

from src.infrastructure.documents.models import Document, ImageRef
from src.services.extraction.prompt import (
    MAX_CHUNK_CHARS,
    MAX_IMAGES_PER_CALL,
    OVERLAP_CHARS,
)


@dataclass(frozen=True, slots=True)
class Part:
    """One call's worth of a document."""

    texts: list[tuple[str, str]] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)

    @property
    def labels(self) -> set[str]:
        return {label for label, _ in self.texts} | {image.origin for image in self.images}

    @property
    def label(self) -> str:
        """What an item cites when its own citation does not match anything."""
        return next(iter(sorted(self.labels)), "")

    @property
    def tail(self) -> str:
        """The end of this part, shown to the next one as already transcribed."""
        return self.texts[-1][1][-OVERLAP_CHARS:] if self.texts else ""



def split(document: Document) -> list[Part]:
    """Every part of this document, in reading order.

    Text and images are cut separately because a document has one or the other:
    a PDF with a text layer yields pages, and one without yields rendered
    images. A file that somehow has both is read text first, then pictures.
    """
    return _text_parts(document) + _image_parts(document)


def _text_parts(document: Document) -> list[Part]:
    pieces = [(page.origin, page.text) for page in document.pages if page.text.strip()]
    if not pieces and document.text.strip():
        pieces = [(document.origin, document.text)]

    parts: list[Part] = []
    current: list[tuple[str, str]] = []
    size = 0

    for label, text in pieces:
        if current and size + len(text) > MAX_CHUNK_CHARS:
            parts.append(Part(texts=current))
            current, size = [], 0
        current.append((label, text))
        size += len(text)

    if current:
        parts.append(Part(texts=current))
    return parts


def _image_parts(document: Document) -> list[Part]:
    return [
        Part(images=document.images[start : start + MAX_IMAGES_PER_CALL])
        for start in range(0, len(document.images), MAX_IMAGES_PER_CALL)
    ]


