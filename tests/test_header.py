"""B4: the top of the form.

The model is faked, so what is under test is what it is shown and what happens
to its answer - which of its claims get in, which are dropped, and why.
"""

from datetime import UTC, datetime

import pytest

from src.domain.models import EmailAddress, NormalizedEmail, Signals
from src.infrastructure.documents.models import Document, FileKind, Grid
from src.infrastructure.llm.exceptions import LLMCallError
from src.services.extraction import DocumentRole, ReadDocument
from src.services.extraction.header import MAX_WHY_CHARS, HeaderReader
from src.services.extraction.models import (
    EXPLAINED_A_FIELD_IT_FOUND,
    FIELD_CLAIMED_TWICE,
    FIELD_FROM_UNKNOWN_SOURCE,
    FIELD_WITHOUT_SOURCE,
    HEADER_FAILED,
    PLACEHOLDER_VALUE,
    HeaderField,
)
from src.services.extraction.prompt import build_header_messages
from src.services.extraction.schemas import (
    ExtractedField,
    HeaderExtraction,
    MissingField,
)
from tests.fakes import BrokenLLM, FakeLLM

BODY = """\
Dear Sirs,

Kindly quote for MV ALMI GLOBE, IMO 9232395.
Delivery Jebel Ali, ETA 12 Oct. Please quote before 08 Oct.

Best regards,
Nikos Papadopoulos
Purchasing Officer
+30 210 4599 000
"""

EMAIL = NormalizedEmail(
    sender=EmailAddress(name="Nikos Papadopoulos", address="purchasing@almiship.com"),
    subject="RFQ 78432 / MV ALMI GLOBE / Jebel Ali / deck stores",
    body_text=BODY,
    received_at=datetime(2026, 9, 5, 8, 14, tzinfo=UTC),
)

REQUISITION = ReadDocument(
    document=Document(
        filename="Requisition_78432.xlsx",
        kind=FileKind.XLSX,
        grids=[
            Grid(
                origin="Requisition_78432.xlsx#Deck",
                name="Deck",
                rows=[
                    ["VESSEL", "MV ALMI GLOBE"],
                    ["IMO", "9232395"],
                    [],
                    ["ITEM", "IMPA", "DESCRIPTION", "QTY", "UNIT"],
                    ["1", "550101", "ROPE PP 24MM X 220M", "2", "coil"],
                ],
            )
        ],
    ),
    role=DocumentRole.ITEM_GRID,
    what="Requisition listing 1 deck item",
)

PHOTO = ReadDocument(
    document=Document(filename="IMG_2201.png", kind=FileKind.IMAGE),
    role=DocumentRole.SUPPORTING,
    what="Photo of a VHF radio showing its nameplate",
    facts=["ICOM", "IC-M330GE", "S/N 12345678"],
)


def found(field: HeaderField, value: str, source: str = "email.body") -> ExtractedField:
    return ExtractedField(field=field, value=value, source=source)


async def read(*fields: ExtractedField, documents=None, llm=None, not_found=()):
    reader = HeaderReader(
        llm or FakeLLM(HeaderExtraction(fields=list(fields), not_found=list(not_found)))
    )
    return await reader.read(EMAIL, documents if documents is not None else [REQUISITION])


def missing(field: HeaderField, why: str) -> MissingField:
    return MissingField(field=field, why=why)


# --- what gets in ---------------------------------------------------------


async def test_a_found_field_keeps_its_value_and_its_source():
    header = await read(found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE", "email.subject"))

    assert header.value(HeaderField.VESSEL_NAME) == "MV ALMI GLOBE"
    assert header.source(HeaderField.VESSEL_NAME) == "email.subject"


async def test_values_are_not_normalised_on_the_way_out():
    """B6 maps "Jebel Ali" onto the workbook's list, and needs the original."""
    header = await read(
        found(HeaderField.DELIVERY_PORT, "Jebel Ali"),
        found(HeaderField.ETA, "12 Oct"),
    )

    assert header.value(HeaderField.DELIVERY_PORT) == "Jebel Ali"
    assert header.value(HeaderField.ETA) == "12 Oct"


async def test_a_field_may_cite_an_attachment():
    header = await read(
        found(HeaderField.IMO, "9232395", "Requisition_78432.xlsx")
    )

    assert header.value(HeaderField.IMO) == "9232395"
    assert not header.warnings


async def test_a_field_the_model_left_out_is_simply_absent():
    """No nulls, no "N/A" - an absent entry is the honest answer."""
    header = await read(found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"))

    assert header.value(HeaderField.ETD) is None
    assert HeaderField.ETD not in header.fields


# --- what is refused ------------------------------------------------------


@pytest.mark.parametrize("placeholder", ["N/A", "unknown", "-", "TBC", "  "])
async def test_a_placeholder_is_not_a_value(placeholder: str):
    """A slot invites a value; refusing the reflex costs nothing."""
    header = await read(found(HeaderField.ETD, placeholder))

    assert HeaderField.ETD not in header.fields
    assert PLACEHOLDER_VALUE in header.warnings


async def test_a_value_with_no_source_is_dropped():
    """A value that cannot say where it came from was invented."""
    header = await read(found(HeaderField.VESSEL_NAME, "MV GHOST", ""))

    assert HeaderField.VESSEL_NAME not in header.fields
    assert FIELD_WITHOUT_SOURCE in header.warnings


async def test_a_field_claimed_twice_keeps_the_first_answer():
    header = await read(
        found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"),
        found(HeaderField.VESSEL_NAME, "MV OTHER"),
    )

    assert header.value(HeaderField.VESSEL_NAME) == "MV ALMI GLOBE"
    assert FIELD_CLAIMED_TWICE in header.warnings


async def test_a_citation_pointing_at_nothing_we_showed_is_flagged_but_kept():
    """Probably a differently worded citation, possibly not. A person decides."""
    header = await read(found(HeaderField.IMO, "9232395", "my own knowledge"))

    assert header.value(HeaderField.IMO) == "9232395"
    assert FIELD_FROM_UNKNOWN_SOURCE in header.warnings


async def test_a_model_that_cannot_answer_gives_an_empty_header_not_a_guess():
    broken = BrokenLLM(LLMCallError("model", RuntimeError("provider down")))

    header = await HeaderReader(broken).read(EMAIL, [REQUISITION])

    assert header.fields == {}
    assert HEADER_FAILED in header.warnings


# --- what the form still needs -------------------------------------------


async def test_the_starred_fields_that_are_missing_are_named():
    header = await read(
        found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"),
        found(HeaderField.IMO, "9232395"),
    )

    assert not header.is_complete
    assert header.missing_required == [
        HeaderField.RFQ_REFERENCE,
        HeaderField.DELIVERY_PORT,
        HeaderField.CURRENCY,
        HeaderField.RFQ_TYPE,
    ]


# --- why a starred field is not there -------------------------------------


async def test_the_model_may_say_where_it_looked_for_a_field_it_did_not_find():
    """The one account of an absence anything can give. Code sees an empty cell
    and cannot tell "nobody wrote it" from "it is there and I missed it"."""
    header = await read(
        found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"),
        not_found=[missing(HeaderField.IMO, "Not stated anywhere - the vessel is named but never numbered.")],
    )

    assert header.not_found[HeaderField.IMO].startswith("Not stated anywhere")


async def test_a_note_about_a_field_the_same_answer_filled_in_is_thrown_away():
    """Both "here it is" and "it is not there" about one field. The value is
    the half worth keeping, and a note contradicting it would send the desk
    looking for something already on the form."""
    header = await read(
        found(HeaderField.IMO, "9232395"),
        not_found=[missing(HeaderField.IMO, "No IMO number is given.")],
    )

    assert header.value(HeaderField.IMO) == "9232395"
    assert header.not_found == {}
    assert EXPLAINED_A_FIELD_IT_FOUND in header.warnings


async def test_a_note_about_a_field_nobody_starred_is_dropped():
    """A missing phone number needs no paragraph. Only the six cells that
    block the form are worth explaining."""
    header = await read(
        found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"),
        not_found=[missing(HeaderField.CUSTOMER_PHONE, "No telephone number in the signature.")],
    )

    assert header.not_found == {}


async def test_a_note_is_kept_to_one_sentence():
    """A model that writes a paragraph is explaining rather than answering, and
    the desk reads the first line of it anyway."""
    header = await read(
        found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"),
        not_found=[missing(HeaderField.CURRENCY, "No currency. " * 60)],
    )

    assert len(header.not_found[HeaderField.CURRENCY]) <= MAX_WHY_CHARS


async def test_an_empty_note_is_not_a_note():
    header = await read(
        found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"),
        not_found=[missing(HeaderField.CURRENCY, "   ")],
    )

    assert header.not_found == {}


def test_the_prompt_asks_for_the_reason_and_forbids_guessing_in_it():
    messages = build_header_messages(EMAIL, [REQUISITION])
    system = messages[0][1]

    assert "not_found" in system
    assert "Say only what you actually saw" in system


async def test_a_header_with_every_starred_field_is_complete():
    header = await read(
        found(HeaderField.VESSEL_NAME, "MV ALMI GLOBE"),
        found(HeaderField.IMO, "9232395"),
        found(HeaderField.RFQ_REFERENCE, "78432"),
        found(HeaderField.DELIVERY_PORT, "Jebel Ali"),
        found(HeaderField.CURRENCY, "USD"),
        found(HeaderField.RFQ_TYPE, "DECK"),
    )

    assert header.is_complete
    assert header.missing_required == []


# --- what the model is shown ----------------------------------------------


def prompt(documents=None, signals=None) -> str:
    return build_header_messages(
        EMAIL, documents if documents is not None else [REQUISITION], signals
    )[1][1]


def test_the_prompt_carries_the_email_and_who_sent_it():
    text = prompt()

    assert "RFQ 78432 / MV ALMI GLOBE" in text
    assert "purchasing@almiship.com" in text
    assert "Nikos Papadopoulos" in text
    assert "2026-09-05T08:14" in text


def test_the_prompt_carries_the_top_of_the_item_file_not_its_items():
    """The customer's own header block sits above the table; the rows do not
    contain a vessel name and would only cost tokens."""
    text = prompt()

    assert "VESSEL | MV ALMI GLOBE" in text
    assert "IMO | 9232395" in text


def test_the_prompt_carries_what_a_supporting_file_showed():
    text = prompt([REQUISITION, PHOTO])

    assert "Photo of a VHF radio" in text
    assert "IC-M330GE" in text


def test_the_prompt_lists_the_sources_the_model_may_cite():
    text = prompt([REQUISITION, PHOTO])

    assert "email.subject" in text
    assert "Requisition_78432.xlsx" in text
    assert "IMG_2201.png" in text


def test_regex_guesses_go_in_labelled_as_guesses():
    text = prompt(signals=Signals(vessel_name="MV ALMI GLOBE", imo="9232395"))

    assert "<precomputed>" in text
    assert "to be confirmed" in text
    assert "vessel_name: MV ALMI GLOBE" in text


def test_no_precomputed_block_when_regex_found_nothing():
    assert "<precomputed>" not in prompt(signals=Signals())


def test_an_email_with_no_attachments_still_builds_a_prompt():
    text = prompt([])

    assert "(none)" in text
    assert "Kindly quote for MV ALMI GLOBE" in text
