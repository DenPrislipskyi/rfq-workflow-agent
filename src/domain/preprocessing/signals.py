"""L1: pull domain entities out of the text with regex.

Everything here is a hint for `<precomputed_signals>`, never a verdict - the
LLM may override any of it.
"""

import re

from src.domain.enums import AttachmentKind
from src.domain.models import Signals

# One pattern per field, alternatives ordered most to least specific; the first
# group that captures anything wins.
_I = re.IGNORECASE
_IM = re.IGNORECASE | re.MULTILINE

_PATTERNS: dict[str, re.Pattern[str]] = {
    "imo": re.compile(r"\bIMO[:\s#]*(\d{7})\b", _I),
    "vessel_name": re.compile(
        r"^\s*Vessel(?:\s*Name)?\s*:\s*(.+?)\s*$"
        r"|\b(?:VSL|M/T|M/V|MT|MV)[:\s]+([A-Z][A-Za-z0-9\-' ]{2,40})",
        _IM,
    ),
    "rfq_reference": re.compile(
        r"\bRFQ\s*(?:No|Ref|Reference)\s*[.:]?\s*([A-Z0-9][A-Z0-9\-/._]{3,40})", _I
    ),
    "quotation_reference": re.compile(
        r"\bQUOTATION\s*[:#]\s*([A-Z0-9][A-Z0-9\-/.]{3,40})"
        r"|\bQOT\s*Ref(?:erence)?\s*:?\s*(\d[\d.]{4,})",
        _I,
    ),
    "ems_reference": re.compile(r"\bEMS\s*Reference\s*:\s*(\d[\d.]{4,})", _I),
    "delivery_port": re.compile(
        r"\b(?:Delivery\s*Port|Port\s*Name|Port\s*of\s*Call)\s*:\s*([A-Za-z \-]{3,40})", _I
    ),
    "eta": re.compile(
        r"\be\.?t\.?a\.?\b[:\s]{0,3}"
        r"(\d{1,2}[-/\s][A-Za-z0-9]{2,9}[-/\s]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})",
        _I,
    ),
    "quote_due_date": re.compile(
        r"quotation\s+due\s+date\s+is\s+(\d{1,2}/[A-Za-z]{3,9}/\d{4})"
        r"|\bQuote\s*Before\s*:\s*(\d{1,2}[-/][A-Za-z]{3}[-/]\d{4})",
        _I,
    ),
    "department_or_category": re.compile(
        r'Department\s*"([^"]{2,60})"' r"|Requisition\s*Category\s*:\s*([A-Za-z ]{3,40})", _I
    ),
}

_URGENCY = re.compile(r"\b(U\s*R\s*G\s*E\s*N\s*T|urgent|high priority|asap)\b", _I)

# The customer RFQ form template. Nothing else in a filename has ever changed a
# decision, so nothing else is matched.
_RFQ_FORM_RULES: list[tuple[re.Pattern[str], AttachmentKind]] = [
    (re.compile(r"(?i)^E_QUOT.*\.(?:xlsx|xls)$"), AttachmentKind.RFQ_FORM_XLSX),
    (re.compile(r"(?i)^E_QUOT.*\.pdf$"), AttachmentKind.RFQ_FORM_PDF),
]

# Small inline images are signature logos, not content.
_SIGNATURE_IMAGE_NAME = re.compile(r"(?i)^image\d{3}\.")
_SIGNATURE_IMAGE_MAX_BYTES = 20_000


_VESSEL_TAIL = re.compile(r"\s+[-\u2013]\s+|\s*,\s*")


def extract_signals(text: str) -> Signals:
    """Find vessel, references, port, dates and urgency markers in one block."""
    found = {field: _first_group(pattern, text) for field, pattern in _PATTERNS.items()}
    if found["vessel_name"]:
        # "m/t North Star - eta Fujairah ..." - keep the name, drop the rest.
        found["vessel_name"] = _VESSEL_TAIL.split(found["vessel_name"])[0].strip()
    urgency = sorted({match.group(1).upper() for match in _URGENCY.finditer(text)})
    return Signals(**found, urgency_markers=urgency)


def classify_attachment(filename: str, size_bytes: int | None = None) -> AttachmentKind:
    """Infer the attachment type from its name alone - Phase 1 opens no files."""
    if _is_signature_image(filename, size_bytes):
        return AttachmentKind.SIGNATURE_IMAGE

    for pattern, kind in _RFQ_FORM_RULES:
        if pattern.search(filename):
            return kind
    return AttachmentKind.OTHER


def _is_signature_image(filename: str, size_bytes: int | None) -> bool:
    if not _SIGNATURE_IMAGE_NAME.match(filename):
        return False
    return size_bytes is not None and size_bytes < _SIGNATURE_IMAGE_MAX_BYTES


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    """Value of the first group that captured something, across all matches."""
    for match in pattern.finditer(text):
        for value in match.groups():
            if value and value.strip():
                return value.strip()
    return None
