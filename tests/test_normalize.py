"""Text normalisation and header extraction."""

from src.domain.preprocessing.normalize import normalize_text, split_headers
from tests.corpus import load_email_by_id


def test_windows_newlines_become_unix() -> None:
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"


def test_trailing_spaces_are_dropped() -> None:
    assert normalize_text("hello   \nworld\t") == "hello\nworld"


def test_runs_of_blank_lines_collapse() -> None:
    assert normalize_text("a\n\n\n\n\nb") == "a\n\nb"


def test_non_breaking_spaces_become_ordinary_ones() -> None:
    """NFKC is what makes the header and separator patterns match reliably."""
    assert normalize_text("From: someone@example.com") == "From: someone@example.com"


def test_header_block_is_parsed_and_removed() -> None:
    raw = (
        "CAUTION: External Sender. Do not click links.\n"
        "Message Number: 1456098\n"
        "From: purchasing@new-company.com\n"
        "To: OUR COMPANY (UAE) (supply@our-company.com)\n"
        "Sent: Tuesday, May 26, 2026 11:30 (UTC +03:00)\n"
        "Subject: VSL: NORTH STAR\n"
        "Dear Sir/Madam\n"
        "you may find attached our RFQ.\n"
    )
    headers, body = split_headers(raw)

    assert headers["from"] == "purchasing@new-company.com"
    assert headers["subject"] == "VSL: NORTH STAR"
    assert headers["message number"] == "1456098"
    assert body.startswith("Dear Sir/Madam")
    assert "Sent:" not in body


def test_body_that_only_looks_like_a_header_is_left_alone() -> None:
    """Without a From: line it is prose, not a header block."""
    raw = "Subject: this line is part of the message\nand so is this one."
    headers, body = split_headers(raw)

    assert headers == {}
    assert body == raw


def test_email_without_a_header_block_is_untouched() -> None:
    raw = "CAUTION: External Sender. Do not click links.\nDear John,\nGood Day !"
    headers, body = split_headers(raw)

    assert headers == {}
    assert body == raw


def test_real_fixture_yields_the_subject_that_distinguishes_updates() -> None:
    """001 and 002 have identical bodies - only the subject prefix differs."""
    first, _ = split_headers(normalize_text(load_email_by_id("001")))
    updated, _ = split_headers(normalize_text(load_email_by_id("002")))

    assert not first["subject"].startswith("[Updated]")
    assert updated["subject"].startswith("[Updated]")
