"""L1: cut an email into the newest message and the history quoted below it.

Classification must look only at the newest message: in the corpus a nine-line
reply sits on top of 250 lines of quoted history holding the full original RFQ,
and a classifier reading all of it calls that a new RFQ every time. Feed this
the body only - `normalize.split_headers` must have removed the header block
first, or the first boundary lands at line zero.
"""

import re

from src.domain.models import QuotedMessage, SplitThread

# Boundaries confirmed against every fixture in the corpus.
_DIVIDER = re.compile(r"^[_-]{2,}\s*Original Message\s*[_-]{2,}\s*$", re.IGNORECASE)
_MESSAGE_ID = re.compile(r"^\s*Message(?: Number)?:\s*\d{4,}\s*$", re.IGNORECASE)
_CAUTION = re.compile(r"^\s*CAUTION:\s*External\b", re.IGNORECASE)
_ON_WROTE = re.compile(r"^On .{5,120}\bwrote:\s*$")

_FROM = re.compile(r"^\s*From:\s*(\S.*)$", re.IGNORECASE)
_DATE = re.compile(r"^\s*(?:Sent|Date):\s*(\S.*)$", re.IGNORECASE)
_SUBJECT = re.compile(r"^\s*Subject:\s*(.*)$", re.IGNORECASE)

# A header block spreads over a few lines; look this far ahead to recognise one.
_HEADER_LOOKAHEAD = 6
# A banner, an id line and a From: line are one boundary, not three.
_CLUSTER_GAP = 4

SUSPICIOUS_SPLIT = "suspicious_split_empty_latest"


def split_thread(body: str, *, min_latest_chars: int = 15) -> SplitThread:
    """Split a body into the newest message and the messages quoted under it."""
    lines = body.split("\n")
    boundaries = _boundaries(lines)

    if not boundaries:
        return SplitThread(latest_message=body.strip())

    latest = "\n".join(lines[: boundaries[0]]).strip()
    quoted = [
        _parse_quoted(lines[start:end])
        for start, end in zip(boundaries, [*boundaries[1:], len(lines)])
    ]

    warnings = []
    if len(latest) < min_latest_chars:
        # The split probably ate the new text. Keep going, but say so out loud.
        warnings.append(SUSPICIOUS_SPLIT)

    return SplitThread(latest_message=latest, quoted_messages=quoted, warnings=warnings)


def _boundaries(lines: list[str]) -> list[int]:
    """Line numbers where a quoted message starts, one per message."""
    hits = [index for index in range(len(lines)) if _is_boundary(lines, index)]

    starts: list[int] = []
    for index in hits:
        if not starts or index - starts[-1] > _CLUSTER_GAP:
            starts.append(index)
    return starts


def _is_boundary(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _DIVIDER.match(line) or _MESSAGE_ID.match(line) or _ON_WROTE.match(line):
        return True
    if _CAUTION.match(line):
        return _banner_precedes_header(lines, index)
    return _starts_header_block(lines, index)


def _starts_header_block(lines: list[str], index: int) -> bool:
    """A From: line that opens a header block - a date and a subject follow it."""
    if not _FROM.match(lines[index]):
        return False
    window = lines[index + 1 : index + 1 + _HEADER_LOOKAHEAD]
    return any(_DATE.match(line) for line in window) and any(
        _SUBJECT.match(line) for line in window
    )


def _banner_precedes_header(lines: list[str], index: int) -> bool:
    """A CAUTION banner is a boundary only when a header block follows it.

    The same banner also shows up inside a quoted body, where it means nothing.
    """
    window = lines[index + 1 : index + 4]
    return any(_MESSAGE_ID.match(line) or _FROM.match(line) for line in window)


def _parse_quoted(lines: list[str]) -> QuotedMessage:
    """Best-effort sender, date and subject for one quoted block."""
    return QuotedMessage(
        raw="\n".join(lines).strip(),
        from_address=_first_match(_FROM, lines),
        sent_at=_first_match(_DATE, lines),
        subject=_first_match(_SUBJECT, lines),
    )


def _first_match(pattern: re.Pattern[str], lines: list[str]) -> str | None:
    for line in lines[:_HEADER_LOOKAHEAD]:
        match = pattern.match(line)
        if match:
            return match.group(1).strip() or None
    return None
