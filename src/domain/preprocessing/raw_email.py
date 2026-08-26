"""L0: a pasted email dump to the internal shape.

The minimal API mode sends one blob of text: the header block Outlook renders
above the body, then the body. Whatever cannot be recovered stays None and is
named in `parse_warnings`, so a caller sees what the classifier lacked.
"""

import re
from email.utils import getaddresses

from src.domain.models import Attachment, EmailAddress, NormalizedEmail
from src.domain.preprocessing.html import html_to_text
from src.domain.preprocessing.normalize import normalize_text, split_headers
from src.domain.preprocessing.signals import classify_attachment

NO_HEADER_BLOCK = "no_header_block"
DATE_NOT_PARSED = "received_at_not_parsed"

# "OUR COMPANY (supply@our-company.com)" - the corpus puts the address in round
# brackets, which an RFC-aware parser reads as a comment and discards. Rewriting
# them as angle brackets keeps the address.
_BRACKETED_ADDRESS = re.compile(r"\(\s*([^()\s]+@[^()\s]+)\s*\)")


def parse_raw_email(raw_text: str) -> NormalizedEmail:
    """Best-effort parse of a raw dump. Missing pieces are reported, never invented."""
    headers, body = split_headers(normalize_text(raw_text))

    warnings: list[str] = []
    if not headers:
        warnings.append(NO_HEADER_BLOCK)
    if headers.get("sent") or headers.get("date"):
        # "Tuesday, May 26, 2026 11:30 (UTC +03:00)" reads in no stdlib parser,
        # and a wrong timestamp is worse than none.
        warnings.append(DATE_NOT_PARSED)

    return NormalizedEmail(
        sender=first_address(headers.get("from")),
        to=parse_addresses(headers.get("to")),
        cc=parse_addresses(headers.get("cc")),
        subject=headers.get("subject") or None,
        body_text=body,
        attachments=parse_attachments(headers.get("attachments")),
        parse_warnings=warnings,
    )


def parse_addresses(value: str | None) -> list[EmailAddress]:
    """Read a From/To/Cc header, tolerating both `Name <a@b>` and `Name (a@b)`."""
    if not value:
        return []

    prepared = _BRACKETED_ADDRESS.sub(r"<\1>", value)
    return [
        EmailAddress(name=name.strip() or None, address=address.lower())
        for name, address in getaddresses([prepared])
        if address
    ]


def first_address(value: str | None) -> EmailAddress | None:
    """The sender header holds one address, however many the parser finds."""
    addresses = parse_addresses(value)
    return addresses[0] if addresses else None


def parse_attachments(value: str | None) -> list[Attachment]:
    """Split the Attachments header and type each file by its name."""
    if not value:
        return []

    return [
        Attachment(filename=name, kind=classify_attachment(name))
        for name in (part.strip() for part in value.split(","))
        if name
    ]


def body_to_text(body_text: str | None, body_html: str | None) -> str:
    """Prefer the plain part; fall back to the HTML one converted to text."""
    if body_text and body_text.strip():
        return normalize_text(body_text)
    if body_html:
        return normalize_text(html_to_text(body_html))
    return ""
