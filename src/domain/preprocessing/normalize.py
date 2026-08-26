"""L0: turn a raw email dump into clean text plus the headers sitting on top of it."""

import re
import unicodedata

# Header keys seen in the corpus: standard Outlook plus our own dump format.
HEADER_KEYS = frozenset(
    {
        "message",
        "message number",
        "from",
        "to",
        "cc",
        "bcc",
        "sent",
        "date",
        "subject",
        "attachments",
        "importance",
        "sensitivity",
    }
)

_CAUTION_BANNER = re.compile(r"(?i)^\s*CAUTION:\s*External\b")
_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z ]{1,18}):\s*(.*)$")


def normalize_text(text: str) -> str:
    """Unicode NFKC, unix newlines, no trailing spaces, no runs of blank lines.

    NFKC also turns non-breaking spaces into ordinary ones, which is what makes
    the header and separator patterns match reliably.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_headers(text: str) -> tuple[dict[str, str], str]:
    """Pull the leading header block off a raw dump.

    Returns lower-cased header keys and the body below them. An email that
    starts straight into prose gets an empty dict and untouched text.
    """
    lines = text.split("\n")
    start = _first_header_line(lines)
    if start is None:
        return {}, text

    headers: dict[str, str] = {}
    index = start
    while index < len(lines):
        match = _HEADER_LINE.match(lines[index])
        if match is None or match.group(1).strip().lower() not in HEADER_KEYS:
            break
        headers[match.group(1).strip().lower()] = match.group(2).strip()
        index += 1

    # Without a sender it is body prose that happens to look like a header.
    if "from" not in headers:
        return {}, text

    return headers, "\n".join(lines[index:]).strip()


def _first_header_line(lines: list[str]) -> int | None:
    """Index of the first header line, skipping a leading banner and blanks."""
    for index, line in enumerate(lines):
        if not line.strip() or _CAUTION_BANNER.match(line):
            continue
        match = _HEADER_LINE.match(line)
        if match and match.group(1).strip().lower() in HEADER_KEYS:
            return index
        return None
    return None
