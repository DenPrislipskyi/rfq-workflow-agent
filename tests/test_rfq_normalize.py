"""B6: what the customer wrote becomes what the cell can hold.

Exact answers throughout - a check digit, a calendar, eight currency codes - so
every case here is an exact assertion and no model appears.
"""

from datetime import date

import pytest

from src.services.extraction.models import (
    DATE_AMBIGUOUS,
    DATE_IMPOSSIBLE,
    DATE_UNREADABLE,
    IMO_INVALID,
    RFQ_TYPE_FELL_BACK,
    HeaderField,
    HeaderValue,
    RfqHeader,
)
from src.services.extraction.normalize import (
    CURRENCY_CODES,
    RFQ_TYPES,
    normalize_currency,
    normalize_header,
    normalize_imo,
    normalize_rfq_type,
    parse_date,
)

TODAY = date(2026, 9, 5)


def header(**fields: str) -> RfqHeader:
    return RfqHeader(
        fields={
            HeaderField(name): HeaderValue(value=value, source="email.body")
            for name, value in fields.items()
        }
    )


def run(**fields: str):
    return normalize_header(header(**fields), today=TODAY)


# --- what the customer wrote is what goes in ------------------------------


def test_a_port_goes_in_exactly_as_the_customer_wrote_it():
    """Real: an RFQ for Gdansk. No list is consulted, so none can refuse it."""
    assert run(delivery_port="Gdansk").text[HeaderField.DELIVERY_PORT] == "Gdansk"


def test_a_vessel_name_is_not_touched():
    assert run(vessel_name="M/V ATLANTIC MOON").text[HeaderField.VESSEL_NAME] == "M/V ATLANTIC MOON"


# --- IMO ------------------------------------------------------------------


def test_a_valid_imo_survives():
    assert normalize_imo("9232395") == "9232395"
    assert normalize_imo("IMO 9232395") == "9232395"


def test_an_imo_whose_check_digit_fails_is_dropped():
    """The check digit catches a transposed pair, which is the mistake people make."""
    assert normalize_imo("9425712") is None


@pytest.mark.parametrize("wrong", ["923239", "92323955", "", "ALMI HYDRA"])
def test_anything_that_is_not_seven_digits_is_dropped(wrong: str):
    assert normalize_imo(wrong) is None


def test_an_imo_that_fails_its_check_digit_still_goes_in_flagged():
    """"If mismatch -> still populate extracted IMO + flag", says the mapping
    document. A number with a typo still tells a person which vessel was meant."""
    result = run(imo="9425712")

    assert result.text[HeaderField.IMO] == "9425712"
    assert result.checked[HeaderField.IMO] == IMO_INVALID


# --- currency -------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [("USD", "USD"), ("usd", "USD"), ("$", "USD"), ("DHS", "AED"),
     ("USD-US DOLLAR", "USD"), ("DHS-Un. Ar Emir. Dirham", "AED")],
)
def test_a_currency_the_template_offers(written: str, expected: str):
    assert normalize_currency(written, CURRENCY_CODES) == expected


def test_a_currency_the_customer_never_named_stays_empty():
    """No branch default. Nothing was said, so nothing is written."""
    assert not run().has(HeaderField.CURRENCY)


# --- RFQ type -------------------------------------------------------------


def test_a_department_the_template_knows():
    assert normalize_rfq_type("ENGINE MATERIAL", RFQ_TYPES) == ("ENGINE", False)


def test_an_unknown_department_uses_the_bucket_the_template_provides():
    """A blank starred field is worse than the bucket put there for the purpose."""
    assert normalize_rfq_type("SPARE PARTS", RFQ_TYPES) == ("OTHERS", True)


def test_falling_back_is_reported():
    assert RFQ_TYPE_FELL_BACK in run(rfq_type="SPARE PARTS").warnings


# --- dates ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("2026-10-12", date(2026, 10, 12)),
        ("12-Oct-2026", date(2026, 10, 12)),
        ("12 October 2026", date(2026, 10, 12)),
        ("Oct 12, 2026", date(2026, 10, 12)),
        ("13/06/2026", date(2026, 6, 13)),
        ("06/13/2026", date(2026, 6, 13)),
    ],
)
def test_dates_that_can_only_be_read_one_way(written: str, expected: date):
    assert parse_date(written, today=TODAY) == (expected, "")


@pytest.mark.parametrize("written", ["03/04/2026", "02/06/2026"])
def test_a_date_that_could_be_read_two_ways_is_left_blank(written: str):
    """A missing ETA costs one question; a wrong one costs a delivery.

    "02/06/2026" is the due date on a real ALMI requisition, and nothing in the
    document says whether it is June or February."""
    assert parse_date(written, today=TODAY) == (None, DATE_AMBIGUOUS)


def test_a_year_that_was_not_written_is_the_one_that_looks_forward():
    assert parse_date("12 Oct", today=TODAY)[0] == date(2026, 10, 12)


def test_a_month_already_well_past_means_next_year():
    assert parse_date("12 Jan", today=TODAY)[0] == date(2027, 1, 12)


def test_a_month_only_just_past_stays_in_this_year():
    """A vessel that arrived last week is still a date somebody meant."""
    assert parse_date("28 Aug", today=TODAY)[0] == date(2026, 8, 28)


# --- a day and a month, with the year left off ----------------------------


@pytest.mark.parametrize("written", ["28/07", "28.07", "28-07", "07/28"])
def test_a_day_and_a_month_with_no_year_is_still_a_date(written: str):
    """Real: "The requested delivery date is 28/07" - example 1, and the only
    date in it. The 28th cannot be a month, so the order is settled, and the
    year comes from the same rule that reads `12 Oct`."""
    assert parse_date(written, today=TODAY) == (date(2026, 7, 28), "")


def test_a_day_and_a_month_already_well_past_means_next_year():
    assert parse_date("15/01", today=TODAY)[0] == date(2027, 1, 15)


@pytest.mark.parametrize("written", ["03/04", "12/07"])
def test_a_day_and_a_month_that_could_be_either_way_round_is_left_blank(written: str):
    """Leaving the year off does not make the order any clearer: `03/04` is
    still the third of April or the fourth of March."""
    assert parse_date(written, today=TODAY) == (None, DATE_AMBIGUOUS)


@pytest.mark.parametrize("written", ["28/07.", "28.07.", "12 Oct.", "28/07 "])
def test_a_date_quoted_out_of_a_sentence_keeps_its_punctuation_out_of_the_way(
    written: str,
):
    """The header is read verbatim, so the sentence's full stop comes with it.
    Nothing of a date is ever in a trailing separator."""
    assert parse_date(written, today=TODAY)[0] is not None


# --- read without trouble, and still not a day ----------------------------


@pytest.mark.parametrize(
    "written",
    ["31/02", "40/07", "28/00", "31/02/2026", "2026-02-31", "31 Feb", "Feb 31 2026"],
)
def test_a_value_that_reads_like_a_date_but_is_not_a_day_says_so(written: str):
    """These used to come back blank with nothing to say, which is the one thing
    this stage must never do - the remark under the form would have named the
    cell and given no reason for it."""
    assert parse_date(written, today=TODAY) == (None, DATE_IMPOSSIBLE)


def test_a_customers_typo_is_told_apart_from_our_own_limit():
    """Different reasons because they send a person to different places: one to
    the customer's file, the other to this parser."""
    assert parse_date("31/02", today=TODAY)[1] != parse_date("on arrival", today=TODAY)[1]


def test_something_that_is_not_a_date_at_all():
    assert parse_date("on arrival", today=TODAY) == (None, DATE_UNREADABLE)


def test_dates_land_in_their_own_dictionary_not_among_the_text():
    result = run(eta="12 Oct", quote_before="2026-10-08", vessel_name="MV ALMI GLOBE")

    assert result.dates[HeaderField.ETA] == date(2026, 10, 12)
    assert result.dates[HeaderField.QUOTE_BEFORE] == date(2026, 10, 8)
    assert result.text[HeaderField.VESSEL_NAME] == "MV ALMI GLOBE"
    assert HeaderField.ETA not in result.text


def test_an_unreadable_date_is_reported():
    result = run(eta="on arrival")

    assert not result.has(HeaderField.ETA)
    assert result.dropped[HeaderField.ETA] == DATE_UNREADABLE


def test_an_impossible_date_is_reported_with_a_reason_of_its_own():
    """A blank cell with an empty reason beside it is what this used to be."""
    result = run(eta="31/02")

    assert not result.has(HeaderField.ETA)
    assert result.dropped[HeaderField.ETA] == DATE_IMPOSSIBLE
    assert result.warnings == [DATE_IMPOSSIBLE]


def test_the_delivery_date_of_example_one_reaches_its_cell():
    """Written `28/07` in the body, and starred nowhere - which is why it went
    unnoticed: the form was complete either way, just missing a date."""
    result = run(requested_delivery="28/07")

    assert result.dates[HeaderField.REQUESTED_DELIVERY] == date(2026, 7, 28)
    assert result.dropped == {}


# --- everything together --------------------------------------------------


def test_a_field_the_header_never_had_is_not_reported_as_dropped():
    """Only a value that was there and did not survive counts as dropped."""
    result = run(vessel_name="MV ALMI GLOBE")

    assert result.dropped == {}
    assert result.warnings == []


# --- who sent it, and therefore the sender code ---------------------------


def test_an_email_that_never_says_who_it_is_from_explains_nothing():
    """There is nothing to explain: the cell is blank because the email is
    silent, and the list of blank starred cells already says that."""
    result = run(vessel_name="MV ALMI GLOBE")

    assert not result.has(HeaderField.SENDER_CODE)
    assert result.dropped == {}
    assert result.warnings == []


# --- everything together --------------------------------------------------


def test_a_full_header_comes_out_ready_for_the_cells():
    """Real values from the RFQ for BORKUM. Nothing is looked up anywhere; the
    only changes are the two a cell forces."""
    result = run(
        vessel_name="BORKUM",
        imo="9937397",
        rfq_reference="0093/2025e-339655",
        delivery_port="Gdansk",
        currency="eur",
        rfq_type="maintenance/overhaul others",
        requested_delivery="28/07",
    )

    assert result.text == {
        HeaderField.VESSEL_NAME: "BORKUM",
        HeaderField.IMO: "9937397",
        HeaderField.RFQ_REFERENCE: "0093/2025e-339655",
        HeaderField.DELIVERY_PORT: "Gdansk",
        HeaderField.CURRENCY: "EUR",
        HeaderField.RFQ_TYPE: "OTHERS",
    }
    assert result.dates == {HeaderField.REQUESTED_DELIVERY: date(2026, 7, 28)}
    assert result.dropped == {}
