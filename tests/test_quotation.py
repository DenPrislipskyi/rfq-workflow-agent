"""The quotation PDF: what it says, and that it says it on the desk's form.

Read back with `pypdf`, as text. The layout itself is checked by eye against
`docs/samples/PDFQuote*.pdf`; what these tests hold down is the part a customer
would act on - every line, every number, and the dates.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from src.services.quotation import DUBAI, SINGAPORE, Quotation, QuotedLine, render

LOGO = Path(__file__).parents[1] / "config" / "quotation_pdf_logo.jpg"


def line(number: int = 1, **overrides) -> QuotedLine:
    fields = {
        "number": number,
        "code": "T69030200",
        "description": "HEX HEAD BOLT STEEL UNGALV, M12 X 50MM",
        "quantity": "100",
        "uom": "set",
        "unit_price": Decimal("48.65"),
    }
    return QuotedLine(**{**fields, **overrides})


def quotation(*lines: QuotedLine, **overrides) -> Quotation:
    fields = {
        "issuer": SINGAPORE,
        "number": "RFQ-0002",
        "customer": "purchasing@example.com",
        "customer_email": "purchasing@example.com",
        "port": "Singapore",
        "vessel": "MT ODENSE",
        "imo": "9301744",
        "received_on": date(2026, 9, 16),
        "approved_at": datetime(2026, 9, 25, 16, 14, tzinfo=UTC),
        "lines": lines or (line(),),
    }
    return Quotation(**{**fields, **overrides})


def text_of(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)


def pages_of(pdf: bytes) -> int:
    return len(PdfReader(BytesIO(pdf)).pages)


def test_it_is_a_pdf():
    assert render(quotation()).startswith(b"%PDF")


def test_it_goes_out_on_the_singapore_letterhead():
    text = text_of(render(quotation(), logo=LOGO))

    assert "Seven Seas Maritime Services (Singapore) Pte. Ltd" in text
    assert "UEN : 199305221C" in text
    assert "Quotation For purchasing@example.com" in text


def test_it_says_who_what_and_where():
    text = text_of(render(quotation()))

    assert "RFQ-0002" in text
    assert "MT ODENSE" in text
    assert "9301744" in text
    assert "Singapore" in text


def test_it_is_dated_as_the_desk_dates_it():
    """`16 Sep 2026`, zero-padded like the desk's own - and valid for the same
    thirty days the preview promises, counted from approval."""
    text = text_of(render(quotation(received_on=date(2026, 8, 1))))

    assert "01 Aug 2026" in text
    assert "25 Oct 2026" in text


def test_every_line_is_quoted_with_our_code_and_our_words():
    text = text_of(render(quotation(line(1), line(2, code="T33116401", description="CHIN STRAP"))))

    assert "T69030200" in text
    assert "HEX HEAD BOLT STEEL UNGALV, M12 X 50MM" in text
    assert "T33116401" in text
    assert "CHIN STRAP" in text


def test_the_numbers_are_printed_as_the_desk_prints_them():
    """Two decimals, no currency sign, no thousands separator - `4865.00`."""
    text = text_of(render(quotation()))

    assert "100.00" in text
    assert "48.65" in text
    assert "4865.00" in text
    assert "Total Price(USD)" in text


def test_the_total_is_the_shown_price_times_the_quantity():
    """The rule the screen follows, so the PDF agrees with it to the cent."""
    assert line(unit_price=Decimal("2.39"), quantity="500").total == Decimal("1195.00")
    assert line(unit_price=Decimal("0.63"), quantity="5").total == Decimal("3.15")


def test_the_subtotal_adds_up_the_lines():
    doc = quotation(
        line(1, unit_price=Decimal("90.74"), quantity="2"),
        line(2, unit_price=Decimal("28.00"), quantity="10"),
    )

    assert doc.subtotal == Decimal("461.48")
    assert "461.48" in text_of(render(doc))


def test_a_quantity_that_is_not_a_number_is_printed_as_written_and_not_totalled():
    """`2 coil` cannot be multiplied, and a blank would hide what was asked."""
    odd = line(quantity="2 coil")

    assert odd.total is None
    assert quotation(odd).subtotal == Decimal(0)
    assert "2 coil" in text_of(render(quotation(odd)))


def test_a_quantity_written_with_a_comma_is_still_a_number():
    assert line(quantity="2,5", unit_price=Decimal("10")).total == Decimal("25.00")


def test_pack_size_is_left_off():
    assert "PackSize" not in text_of(render(quotation()))


def test_what_a_customer_wrote_prints_rather_than_breaking_the_page():
    """ReportLab reads markup in its paragraphs; a `<` or `&` in a description
    must come out as a character, not as a tag."""
    text = text_of(render(quotation(line(description="BOLTS <M12> & NUTS"))))

    assert "BOLTS <M12> & NUTS" in text


def test_a_long_rfq_runs_onto_more_pages_and_says_so():
    many = tuple(line(number) for number in range(1, 81))
    pdf = render(quotation(*many))

    assert pages_of(pdf) > 1
    text = text_of(pdf)
    assert f"Page 1 of {pages_of(pdf)}" in text
    # The column names come back on every page: numbers without them are a
    # page nobody can read on its own.
    assert text.count("Identification") == pages_of(pdf)


def test_a_missing_logo_does_not_stop_the_quotation():
    assert render(quotation(), logo=Path("/nowhere/logo.jpg")).startswith(b"%PDF")


def test_the_dubai_office_signs_its_own_letterhead():
    text = text_of(render(quotation(issuer=DUBAI)))

    assert "Seven Seas Shipchandlers (L.L.C)" in text
    assert "P.O.Box 5592, Dubai, United Arab Emirates" in text
    assert "supply.uae@sevenseasgroup.com" in text
    assert "UEN" not in text, "the Dubai form carries no Singapore registration"
    assert "Singapore) Pte" not in text


def test_the_dubai_form_carries_discount_and_vat_columns():
    text = text_of(render(quotation(issuer=DUBAI)))

    # `Discount ( - )` wraps onto two lines in its narrow column, as on the
    # desk's own form, so its first word is what is looked for.
    for heading in ("Rate", "Discount", "Amount", "VAT%", "Tax Amount", "Net Total"):
        assert heading in text
    assert "PackSize" not in text and "Pack Size" not in text


def test_the_dubai_form_prints_zero_discount_and_zero_vat():
    """Nothing we hold says a line is discounted or taxed, and the desk's own
    UAE quotation prints zeros there too."""
    doc = quotation(line(quantity="5", unit_price=Decimal("544.23")), issuer=DUBAI)
    text = text_of(render(doc))

    assert "0.00%" in text
    assert "2721.15" in text
    assert doc.vat == Decimal("0.00")
    assert doc.grand_total == doc.subtotal == Decimal("2721.15")


def test_the_dubai_totals_have_a_vat_line_and_singapore_s_do_not():
    assert "\nVAT" in text_of(render(quotation(issuer=DUBAI)))
    assert "\nVAT" not in text_of(render(quotation(issuer=SINGAPORE)))


def test_a_line_s_net_is_its_amount_while_nothing_is_taxed():
    one = line(quantity="5", unit_price=Decimal("544.23"))

    assert (one.amount, one.tax, one.net) == (
        Decimal("2721.15"),
        Decimal("0.00"),
        Decimal("2721.15"),
    )

