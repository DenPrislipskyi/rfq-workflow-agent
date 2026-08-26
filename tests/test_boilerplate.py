"""Boilerplate stripping - disclaimers out, signal in."""

from src.domain.preprocessing.boilerplate import strip_boilerplate


def test_caution_banner_is_removed() -> None:
    text, applied = strip_boilerplate("CAUTION: External Sender. Do not click.\nDear John,")
    assert text == "Dear John,"
    assert applied == ["caution_banner"]


def test_confidentiality_notice_and_everything_after_it_goes() -> None:
    text, applied = strip_boilerplate(
        "Please quote 10 oil filters.\nCONFIDENTIALITY & DISCLAIMER NOTICE\nblah blah blah"
    )
    assert text == "Please quote 10 oil filters."
    assert "confidentiality_notice" in applied


def test_signature_is_cut_but_the_sender_details_stay() -> None:
    text, applied = strip_boilerplate(
        "we need 100% Isopropanol.\n"
        "Kind regards,\n"
        "James Miller\n"
        "Purchasing Dept\n"
        "NEW COMPANY LTD\n"
        "Tel: +00 000 000\n"
        "some other trailing junk\n"
        "and more junk\n"
    )
    assert "James Miller" in text
    assert "Purchasing Dept" in text
    assert "and more junk" not in text
    assert "signature" in applied


def test_signature_is_kept_when_prices_follow_it() -> None:
    """Cutting noise is cheap; cutting a price is a missed RFQ."""
    text, applied = strip_boilerplate(
        "Thanks for your enquiry.\nRegards,\nAkhila\nOasis\nIso Propyl Alcohol - price AED 100/can\n"
    )
    assert "AED 100" in text
    assert "signature" not in applied


def test_clean_text_is_returned_unchanged() -> None:
    text, applied = strip_boilerplate("Dear Sirs,\nplease quote 10 oil filters.")
    assert text == "Dear Sirs,\nplease quote 10 oil filters."
    assert applied == []
