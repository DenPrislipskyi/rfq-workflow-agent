"""The whole of stage B, end to end with fake models.

Everything below the pipeline is real: the attachments are read by stage A, the
tables are found and measured, the rows are copied by the loop, and the header
is mapped onto the workbook's own lists. Only the four model calls are faked -
and each fake answers exactly what a model looking at that prompt would.
"""

from datetime import UTC, datetime

from src.domain.models import EmailAddress, NormalizedEmail
from src.infrastructure.documents import Budget, DocumentLoader, SourceFile
from src.services.extraction import (
    ExtractionPipeline,
    FileReader,
    HeaderField,
    HeaderReader,
    body_document)
from src.services.extraction.models import (
    DUPLICATE_ITEM_SOURCE,
    NO_ITEMS_ANYWHERE,
    SAME_ITEM_TWO_QUANTITIES,
    ItemField)
from src.services.extraction.schemas import (
    FoundValue,
    ColumnAssignment,
    TableMapping,
    ExtractedField,
    FileRead,
    HeaderExtraction,
    ItemsFromText,
    ReadItem)
from tests import attachments_builder as build
from tests.fakes import FakeLLM


EMAIL = NormalizedEmail(
    sender=EmailAddress(name="Nikos Papadopoulos", address="purchasing@almiship.com"),
    subject="RFQ 78432 / MV ALMI GLOBE / Jebel Ali",
    body_text="Kindly quote for MV ALMI GLOBE, IMO 9232395. Delivery Jebel Ali, ETA 12 Oct.",
    received_at=datetime(2026, 9, 5, 8, 14, tzinfo=UTC))

HEADER_ANSWER = HeaderExtraction(
    fields=[
        ExtractedField(field=HeaderField.VESSEL_NAME, value="MV ALMI GLOBE", source="email.subject"),
        ExtractedField(field=HeaderField.IMO, value="9232395", source="email.body"),
        ExtractedField(field=HeaderField.RFQ_REFERENCE, value="78432", source="email.subject"),
        ExtractedField(field=HeaderField.DELIVERY_PORT, value="Jebel Ali", source="email.body"),
        ExtractedField(field=HeaderField.ETA, value="12 Oct", source="email.body"),
        ExtractedField(field=HeaderField.CURRENCY, value="$", source="email.body"),
        ExtractedField(field=HeaderField.RFQ_TYPE, value="DECK STORES", source="email.subject"),
    ],
    customer_company=FoundValue(value="Almi Tankers SA", source="email.body"))

GRID_MAPPING = TableMapping(
    header_row=3,
    assignments=[
        ColumnAssignment(column="A", field=ItemField.SR_NO),
        ColumnAssignment(column="B", field=ItemField.CUSTOMER_ITEM_CODE),
        ColumnAssignment(column="C", field=ItemField.DESCRIPTION),
        ColumnAssignment(column="D", field=ItemField.QUANTITY),
        ColumnAssignment(column="E", field=ItemField.UOM),
    ])


class Router:
    """One fake standing in for all four calls, answering by schema.

    The pipeline asks four different questions of two configured models; a
    single double that answers each by the shape it was asked for keeps the test
    about the chain rather than about wiring.
    """

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def invoke(self, messages, schema):
        self.asked.append(schema.__name__)
        answer = self.answers.get(schema.__name__)
        if answer is None:
            raise AssertionError(f"nothing configured for {schema.__name__}")
        if callable(answer):
            answer = answer(messages)
        return await FakeLLM(answer).invoke(messages, schema)


def pipeline(llm) -> ExtractionPipeline:
    return ExtractionPipeline(
        files=FileReader(llm), header=HeaderReader(llm)
    )


async def extract(llm, email, documents, **kwargs):
    """The two steps in the order the handler takes them.

    Reading the files is a step of its own because the triage verdict sits
    between the two: by the time `run` is called every attachment has been read
    once, and reading them again would be a model call per file spent twice.
    """
    agent = pipeline(llm)
    return await agent.run(email, await agent.read(documents), **kwargs)


def read_files(*files: tuple[str, bytes]):
    return DocumentLoader(Budget()).load(
        [SourceFile(filename=name, data=data, size_bytes=len(data)) for name, data in files]
    )


def review_by_filename(**roles: bool):
    """One `FileRead` per file, keyed by a fragment of its name.

    A file said to hold the item list comes back with the column mapping in the
    same answer - that is the whole point of the merged call.
    """

    def answer(messages):
        text = messages[1][1]
        text = text if isinstance(text, str) else text[0]["text"]
        for fragment, holds in roles.items():
            if fragment in text:
                return FileRead(
                    what=f"file matching {fragment}",
                    has_item_list=holds,
                    tables=[GRID_MAPPING] if holds else [])
        return FileRead(what="something else", has_item_list=False)

    return answer


# --- the ordinary case ----------------------------------------------------


async def test_an_email_with_a_spreadsheet_yields_items_and_a_filled_header():
    llm = Router(
        FileRead=review_by_filename(Requisition=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(("Requisition.xlsx", build.xlsx_requisition()))

    result = await extract(llm, 
        EMAIL, documents, today=datetime(2026, 9, 5).date()
    )

    assert [item.description for item in result.items] == [
        "ROPE PP 24MM X 220M",
        "PAINT MARINE WHITE 20L",
        "GASKET SET, PUMP (see drawing)",
    ]
    assert [item.sr_no for item in result.items] == [1, 2, 3]


async def test_the_header_comes_out_as_the_customer_wrote_it():
    llm = Router(
        FileRead=review_by_filename(Requisition=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(("Requisition.xlsx", build.xlsx_requisition()))

    result = await extract(llm, 
        EMAIL, documents, today=datetime(2026, 9, 5).date()
    )

    assert result.normalized.text[HeaderField.DELIVERY_PORT] == "Jebel Ali"
    assert result.normalized.text[HeaderField.CURRENCY] == "USD"
    assert result.normalized.text[HeaderField.RFQ_TYPE] == "DECK"
    assert result.normalized.text[HeaderField.IMO] == "9232395"
    assert result.normalized.dates[HeaderField.ETA] == datetime(2026, 10, 12).date()


async def test_the_verbatim_header_is_kept_beside_the_mapped_one():
    """B6 needs the original to map it; a person needs it to check the mapping."""
    llm = Router(
        FileRead=review_by_filename(Requisition=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(("Requisition.xlsx", build.xlsx_requisition()))

    result = await extract(llm, EMAIL, documents)

    assert result.header.value(HeaderField.DELIVERY_PORT) == "Jebel Ali"
    assert result.header.source(HeaderField.DELIVERY_PORT) == "email.body"


# --- the body of the email is just another attachment ---------------------


def test_the_email_body_becomes_a_document():
    document = body_document(EMAIL)

    assert document.origin == "email.body"
    assert "MV ALMI GLOBE" in document.text


async def test_a_list_typed_into_the_email_is_read_like_any_other_source():
    """No separate path for it: B2 routes it, B5 reads it, it cites itself."""
    typed = EMAIL.model_copy(
        update={
            "body_text": "Please quote:\n1) Rope 24mm x 220m - 2 coils\n"
            "2) Paint white 20L - 5 cans"
        }
    )
    llm = Router(
        FileRead=FileRead(
            what="the email itself",
            has_item_list=True,
            items=[
                ReadItem(description="Rope 24mm x 220m", quantity="2", uom="coils"),
                ReadItem(description="Paint white 20L", quantity="5", uom="cans"),
            ]),
        HeaderExtraction=HEADER_ANSWER)

    result = await extract(llm, typed, [])

    assert [item.description for item in result.items] == [
        "Rope 24mm x 220m",
        "Paint white 20L",
    ]
    assert result.items[0].source == "email.body"


# --- several attachments --------------------------------------------------


async def test_the_same_requisition_attached_twice_is_read_once():
    """A PDF and the spreadsheet it was printed from. Reading both doubles
    every line."""
    llm = Router(
        FileRead=review_by_filename(Requisition=True, copy=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    same = build.xlsx_requisition()
    documents = read_files(("Requisition.xlsx", same), ("copy.xlsx", same))

    result = await extract(llm, EMAIL, documents)

    assert len(result.items) == 3
    assert DUPLICATE_ITEM_SOURCE in result.warnings


def revision(rope: str, *, name: str) -> tuple[str, bytes]:
    """The same three-item requisition with one quantity changed.

    Which is what a revision is: the customer resends the list, one number
    different, and attaches the previous version "for reference".
    """
    rows = [
        ["REQUISITION 7712/26", None, None, None, None],
        [None, None, None, None, None],
        ["ITEM", "IMPA", "DESCRIPTION", "QTY", "UNIT"],
        [1, "550101", "ROPE PP 24MM X 220M", rope, "coil"],
        [2, "232101", "PAINT MARINE WHITE 20L", "5", "can"],
        [3, "311204", "GASKET SET, PUMP (see drawing)", "1", "set"],
    ]
    return name, build.xlsx_of(rows)


async def test_a_revision_attached_beside_the_list_it_replaces_is_reported():
    """Measured on a live run: an `[Updated]` RFQ with the previous version
    forwarded "for reference". Two rows were identical and dropped; the
    mooring rope - 2 coils, then 4 - is the same article at two quantities, and
    it went into the grid twice with nothing said about it. The desk would have
    quoted six coils."""
    llm = Router(
        FileRead=review_by_filename(updated=True, previous=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(
        revision("4", name="updated.xlsx"), revision("2", name="previous.xlsx")
    )

    result = await extract(llm, EMAIL, documents)

    assert len(result.conflicts) == 1, result.conflicts
    assert "ROPE PP 24MM X 220M" in result.conflicts[0]
    assert '"updated.xlsx"' in result.conflicts[0]
    assert '"previous.xlsx"' in result.conflicts[0]
    assert SAME_ITEM_TWO_QUANTITIES in result.warnings


async def test_both_rows_stay_in_the_grid():
    """Not resolved, on purpose: which revision stands is written in the
    covering sentence, not in the files. Dropping the quantity the customer
    meant is as expensive as doubling it, so a person decides."""
    llm = Router(
        FileRead=review_by_filename(updated=True, previous=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(
        revision("4", name="updated.xlsx"), revision("2", name="previous.xlsx")
    )

    result = await extract(llm, EMAIL, documents)
    ropes = [item for item in result.items if "ROPE PP" in (item.description or "")]

    assert [item.quantity for item in ropes] == ["4", "2"]


async def test_the_same_article_twice_inside_one_file_is_the_customers_own_row():
    """A split delivery, or two departments on one list. Measured on both
    finished RFQs the desk sent us - 222 rows, not one article repeated - so a
    repeat inside one file is the customer's business, not a disagreement."""
    rows = [
        ["REQUISITION 7712/26", None, None, None, None],
        [None, None, None, None, None],
        ["ITEM", "IMPA", "DESCRIPTION", "QTY", "UNIT"],
        [1, "550101", "ROPE PP 24MM X 220M", "2", "coil"],
        [2, "550101", "ROPE PP 24MM X 220M", "5", "coil"],
    ]
    llm = Router(
        FileRead=review_by_filename(Requisition=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(("Requisition.xlsx", build.xlsx_of(rows)))

    result = await extract(llm, EMAIL, documents)

    assert result.conflicts == []
    assert [item.quantity for item in result.items] == ["2", "5"]


async def test_a_supporting_photo_does_not_become_items():
    llm = Router(
        FileRead=review_by_filename(Requisition=True, photo=False),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(
        ("Requisition.xlsx", build.xlsx_requisition()),
        ("photo.png", build.png_photo((400, 300))))

    result = await extract(llm, EMAIL, documents)

    assert len(result.items) == 3


async def test_what_a_supporting_file_showed_is_kept_for_a_person_to_see():
    llm = Router(
        FileRead=review_by_filename(
            Requisition=True, photo=False
        ),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(
        ("Requisition.xlsx", build.xlsx_requisition()),
        ("photo.png", build.png_photo((400, 300))))

    result = await extract(llm, EMAIL, documents)

    photo = next(item for item in result.documents if "photo" in item.origin)
    assert not photo.holds_items
    assert photo.what


# --- what is reported when something is missing ---------------------------


async def test_an_email_with_no_items_anywhere_says_so():
    llm = Router(
        FileRead=FileRead(what="a covering letter", has_item_list=False),
        HeaderExtraction=HEADER_ANSWER)

    result = await extract(llm, EMAIL, [])

    assert result.items == []
    assert NO_ITEMS_ANYWHERE in result.warnings
    assert not result.is_complete


async def test_a_starred_field_the_email_never_mentioned_is_named():
    """A cell nobody could fill is what the person checking the forward needs
    to be told about, and the list of them is what decides `is_complete`."""
    without_currency = HeaderExtraction(
        fields=[
            found for found in HEADER_ANSWER.fields if found.field is not HeaderField.CURRENCY
        ],
        customer_company=HEADER_ANSWER.customer_company)
    llm = Router(
        FileRead=review_by_filename(Requisition=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=without_currency)
    documents = read_files(("Requisition.xlsx", build.xlsx_requisition()))

    result = await extract(llm, EMAIL, documents)

    assert "currency" in result.missing_required
    assert not result.is_complete


async def test_a_port_outside_every_branch_still_reaches_the_form():
    """Real: an RFQ for Gdansk. What the customer wrote is what goes in."""
    elsewhere = HeaderExtraction(
        fields=[
            found
            for found in HEADER_ANSWER.fields
            if found.field is not HeaderField.DELIVERY_PORT
        ]
        + [ExtractedField(field=HeaderField.DELIVERY_PORT, value="Gdansk", source="email.body")],
        customer_company=HEADER_ANSWER.customer_company)
    llm = Router(
        FileRead=review_by_filename(Requisition=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=elsewhere)
    documents = read_files(("Requisition.xlsx", build.xlsx_requisition()))

    result = await extract(llm, EMAIL, documents)

    assert result.normalized.text[HeaderField.DELIVERY_PORT] == "Gdansk"
    assert "delivery_port" not in result.missing_required


async def test_a_complete_rfq_is_complete():
    llm = Router(
        FileRead=review_by_filename(Requisition=True),
        TableMapping=GRID_MAPPING,
        HeaderExtraction=HEADER_ANSWER)
    documents = read_files(("Requisition.xlsx", build.xlsx_requisition()))

    result = await extract(llm, EMAIL, documents)

    assert result.is_complete
    assert result.missing_required == []
