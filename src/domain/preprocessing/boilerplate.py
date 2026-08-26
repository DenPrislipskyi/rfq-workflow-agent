"""L1: drop banners, disclaimers and signatures from one message block.

Run this per message after `thread.split_thread`: a disclaimer belongs to the
message it trails, and cutting earlier would erase the separators. The bias is
conservative on purpose - leaving noise in costs tokens, cutting a signal costs
a missed RFQ.
"""

import re

# Single lines that carry no information anywhere they appear.
_LINE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("caution_banner", re.compile(r"(?i)^\s*CAUTION:\s*External\b")),
    ("sensitivity_marker", re.compile(r"(?i)^\s*Sensitivity:\s*\w+\s*$")),
]

# Markers after which the rest of the block is legal or marketing filler.
_TAIL_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("confidentiality_notice", re.compile(r"(?i)CONFIDENTIALITY\s*&?\s*DISCLAIMER")),
    ("generic_disclaimer", re.compile(r"(?i)This (?:message|e-?mail) and any associated files")),
    ("msc_legal_disclaimer", re.compile(r"(?i)MSC LEGAL DISCLAIMER")),
    ("personal_data_notice", re.compile(r"(?i)processing of your personal data")),
    ("holiday_update", re.compile(r"(?i)^\s*HOLIDAY UPDATE\b")),
    ("marcura_footer", re.compile(r"(?i)©\s*20\d\d\s*Marcura")),
]

_SIGN_OFF = re.compile(
    r"(?i)^\s*(?:kind regards|best regards|warm regards|regards|thanks|thank you|"
    r"with kind regards|yours (?:sincerely|faithfully))[,!.]?\s*$"
)

# Never cut a region holding one of these - it may be the signal we came for.
_VALUE_BEARING = re.compile(
    r"(?i)\b(?:qty|quantity|price|amount|uom|imo|eta|etd|vessel|aed|usd|eur|rfq)\b"
)

# Lines kept after a sign-off: name and job title help decide the direction.
_SIGNATURE_KEEP_LINES = 4


def strip_boilerplate(text: str) -> tuple[str, list[str]]:
    """Clean one message block. Returns the text and the rules that fired."""
    applied: list[str] = []

    for name, pattern in _LINE_RULES:
        text, fired = _drop_lines(text, pattern)
        if fired:
            applied.append(name)

    for name, pattern in _TAIL_RULES:
        text, fired = _drop_tail(text, pattern)
        if fired:
            applied.append(name)

    text, fired = _drop_signature(text)
    if fired:
        applied.append("signature")

    return text.strip(), applied


def _drop_lines(text: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    lines = text.split("\n")
    kept = [line for line in lines if not pattern.match(line)]
    return "\n".join(kept), len(kept) != len(lines)


def _drop_tail(text: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    """Cut from the first line matching `pattern` to the end of the block."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if pattern.search(line):
            return "\n".join(lines[:index]), True
    return text, False


def _drop_signature(text: str) -> tuple[str, bool]:
    """Cut everything past a sign-off, keeping the few lines that name the sender."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not _SIGN_OFF.match(line):
            continue

        keep_until = _end_of_signature(lines, index)
        removed = "\n".join(lines[keep_until:])
        if not removed.strip():
            return text, False
        if _VALUE_BEARING.search(removed):
            return text, False  # prices or a vessel name below the sign-off
        return "\n".join(lines[:keep_until]), True

    return text, False


def _end_of_signature(lines: list[str], sign_off: int) -> int:
    """Index just past the sign-off plus the next few non-empty lines."""
    kept = 0
    index = sign_off + 1
    while index < len(lines) and kept < _SIGNATURE_KEEP_LINES:
        if lines[index].strip():
            kept += 1
        index += 1
    return index
