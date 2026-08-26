"""Regex signal extraction and attachment classification."""

import pytest

from src.domain.enums import AttachmentKind
from src.domain.preprocessing.signals import classify_attachment, extract_signals


def test_vessel_name_stops_before_the_rest_of_the_sentence() -> None:
    """Corpus line: "m/t North Star - eta Fujairah subject to Hormuz ..."."""
    signals = extract_signals("m/t North Star - eta Fujairah subject to Hormuz straight opening")
    assert signals.vessel_name == "North Star"


def test_vessel_name_from_a_labelled_line() -> None:
    assert extract_signals("Vessel Name: BLUE HORIZON").vessel_name == "BLUE HORIZON"


def test_imo_is_seven_digits() -> None:
    assert extract_signals("IMO: 9241061").imo == "9241061"
    assert extract_signals("order 12345 pieces").imo is None


def test_references_are_picked_up() -> None:
    signals = extract_signals(
        "RFQ No: BH/O-0001/RFQ26\nQUOTATION: 0015-AB000001C\nEMS Reference: 1.11.2026.00002"
    )
    assert signals.rfq_reference == "BH/O-0001/RFQ26"
    assert signals.quotation_reference == "0015-AB000001C"
    assert signals.ems_reference == "1.11.2026.00002"


def test_quote_due_date_in_both_corpus_formats() -> None:
    assert extract_signals("quotation due date is 02/June/2026").quote_due_date == "02/June/2026"
    assert extract_signals("Quote Before : 20-Jun-2026").quote_due_date == "20-Jun-2026"


def test_delivery_port_needs_its_label() -> None:
    assert extract_signals("Delivery Port: Fujairah").delivery_port == "Fujairah"
    assert extract_signals("we sail past Fujairah").delivery_port is None


def test_eta_must_look_like_a_date() -> None:
    """"eta Fujairah subject to Hormuz" is a routing note, not an arrival time."""
    assert extract_signals("ETA 12/Jun/2026").eta == "12/Jun/2026"
    assert extract_signals("eta Fujairah subject to Hormuz").eta is None


def test_department_is_read_out_of_the_quoted_phrase() -> None:
    signals = extract_signals('our RFQ for Department "Engine Materials".')
    assert signals.department_or_category == "Engine Materials"


def test_urgency_markers_include_the_spaced_out_shouting() -> None:
    signals = extract_signals("U R G E N T reply needed, this is high priority")
    assert signals.urgency_markers == ["HIGH PRIORITY", "U R G E N T"]


def test_a_plain_message_yields_no_signals() -> None:
    signals = extract_signals("Dear David, we need 100% Isopropanol, can you please recheck?")
    assert signals.model_dump(exclude={"urgency_markers"}) == {
        field: None for field in signals.model_dump(exclude={"urgency_markers"})
    }


ATTACHMENTS = [
    ("E_QUOT_XLS_0015-AB000001C_S001-01.XLSX", AttachmentKind.RFQ_FORM_XLSX),
    ("E_QUOT_0015-AB000001C_S001-01.pdf", AttachmentKind.RFQ_FORM_PDF),
    # Everything that is not an RFQ form is OTHER: no decision has ever turned
    # on whether an attachment was a spreadsheet, a drawing or an archive.
    ("prices.xlsx", AttachmentKind.OTHER),
    ("offer.pdf", AttachmentKind.OTHER),
    ("IPA_MSDS.pdf", AttachmentKind.OTHER),
    ("bundle.zip", AttachmentKind.OTHER),
    ("mystery.xyz", AttachmentKind.OTHER),
]


@pytest.mark.parametrize(("filename", "expected"), ATTACHMENTS, ids=[f for f, _ in ATTACHMENTS])
def test_attachment_kind_from_the_filename(filename: str, expected: AttachmentKind) -> None:
    assert classify_attachment(filename) is expected


def test_small_inline_image_is_a_signature_logo() -> None:
    assert classify_attachment("image001.png", size_bytes=4_000) is AttachmentKind.SIGNATURE_IMAGE


def test_large_inline_image_is_real_content() -> None:
    """Only a small image00X.png is a logo; a big one is something the sender attached."""
    assert classify_attachment("image001.png", size_bytes=500_000) is AttachmentKind.OTHER
