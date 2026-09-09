"""Phase 2: read one RFQ email into a populated extraction.

B1 chose the models. `FileReader` reads each attachment in a single call - what
it is, and either what its columns mean or what its items say. `HeaderReader`
reads the form's top from the email and every file at once. `normalize_header`
maps the result onto what the workbook accepts, without a model.

`ExtractionPipeline` is the chain. An ordinary RFQ costs three model calls.
"""

from src.services.extraction.file_reader import FileReader
from src.services.extraction.header import HeaderReader
from src.services.extraction.models import (
    DocumentRole,
    HeaderField,
    HeaderValue,
    ItemField,
    LineItem,
    NormalizedHeader,
    ReadDocument,
    RfqHeader,
)
from src.services.extraction.normalize import normalize_header
from src.services.extraction.pipeline import ExtractionPipeline, RfqExtraction, body_document

__all__ = [
    "DocumentRole",
    "ExtractionPipeline",
    "FileReader",
    "HeaderField",
    "HeaderReader",
    "HeaderValue",
    "ItemField",
    "LineItem",
    "NormalizedHeader",
    "ReadDocument",
    "RfqExtraction",
    "RfqHeader",
    "body_document",
    "normalize_header",
]
