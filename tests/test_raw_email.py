"""Parsing a pasted email dump.

The corpus is exactly this format, so the fixtures are the real test: whatever
the parser gets wrong here, the classifier gets wrong in the minimal API mode.
"""

from src.domain.enums import AttachmentKind
from src.domain.preprocessing.raw_email import (
    DATE_NOT_PARSED,
    NO_HEADER_BLOCK,
    parse_addresses,
    parse_attachments,
    parse_raw_email,
)
from tests.corpus import load_email_by_id


# --------------------------------------------------------------------------- #
# Whole fixtures
# --------------------------------------------------------------------------- #


def test_a_fixture_with_a_header_block_is_fully_recovered() -> None:
    """Expectations are read back out of the fixture, never written down here.

    The corpus is real customer mail and stays out of the repository, so a test
    that hardcoded an address would leak exactly what `.gitignore` protects.
    """
    raw = load_email_by_id("001")
    email = parse_raw_email(raw)

    assert email.sender is not None and email.sender.address
    assert f"From: {email.sender.address}" in raw
    assert email.to and all(item.address for item in email.to)
    assert email.subject and f"Subject: {email.subject}" in raw
    assert "you may find attached our RFQ" in email.body_text
    assert NO_HEADER_BLOCK not in email.parse_warnings


def test_the_header_block_is_removed_from_the_body() -> None:
    """Leaving it in would feed the model the headers twice, once as prose."""
    email = parse_raw_email(load_email_by_id("001"))

    assert not email.body_text.startswith("From:")
    assert "CAUTION: External Sender" not in email.body_text
    assert "Subject:" not in email.body_text


def test_attachments_are_split_and_typed() -> None:
    """Two files on the header line become two typed attachments."""
    email = parse_raw_email(load_email_by_id("001"))

    assert len(email.attachments) == 2
    assert {item.kind for item in email.attachments} == {
        AttachmentKind.RFQ_FORM_PDF,
        AttachmentKind.RFQ_FORM_XLSX,
    }


def test_several_recipients_in_cc_are_all_kept() -> None:
    email = parse_raw_email(load_email_by_id("004"))

    assert len(email.cc) == 2
    assert all("@" in (item.address or "") for item in email.cc)


def test_a_fixture_without_a_header_says_so_instead_of_guessing() -> None:
    """9 of the 13 corpus emails start straight into prose."""
    email = parse_raw_email(load_email_by_id("012"))

    assert email.sender is None
    assert email.subject is None
    assert email.parse_warnings == [NO_HEADER_BLOCK]
    assert email.body_text.startswith("Dear Sirs")


def test_an_unparsed_date_is_reported_rather_than_invented() -> None:
    """"Tuesday, May 26, 2026 11:30 (UTC +03:00)" is no standard format."""
    email = parse_raw_email(load_email_by_id("001"))

    assert email.received_at is None
    assert DATE_NOT_PARSED in email.parse_warnings


# --------------------------------------------------------------------------- #
# Address styles seen in the corpus
# --------------------------------------------------------------------------- #


ADDRESS_STYLES = [
    ("bare", "purchasing@new-company.com", None, "purchasing@new-company.com"),
    (
        "brackets",
        "OUR COMPANY (UAE) (supply@our-company.com)",
        "OUR COMPANY (UAE)",
        "supply@our-company.com",
    ),
    (
        "angles",
        "Our Company Ship Supply - UAE <supply@our-company.com>",
        "Our Company Ship Supply - UAE",
        "supply@our-company.com",
    ),
    (
        "quoted name",
        '"NEW COMPANY LTD Purchasing Dept" <purchasing@new-company.com>',
        "NEW COMPANY LTD Purchasing Dept",
        "purchasing@new-company.com",
    ),
]


def test_every_address_style_in_the_corpus_parses() -> None:
    for style, raw, name, address in ADDRESS_STYLES:
        parsed = parse_addresses(raw)
        assert len(parsed) == 1, style
        assert parsed[0].address == address, style
        assert parsed[0].name == name, style


def test_addresses_are_lower_cased_so_registry_lookups_match() -> None:
    assert parse_addresses("David.Clark@Our-Company.com")[0].address == (
        "david.clark@our-company.com"
    )


def test_a_missing_header_gives_no_addresses_rather_than_an_empty_one() -> None:
    assert parse_addresses(None) == []
    assert parse_addresses("") == []
    assert parse_attachments(None) == []
