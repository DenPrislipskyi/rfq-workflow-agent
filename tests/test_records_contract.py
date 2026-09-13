"""The same questions, asked of both record stores.

`EmailRecords` writes a folder; `DatabaseRecords` writes rows and blobs. The
pipeline is handed one of them and cannot tell which, and the only way that
stays true is to ask them the same things and compare the answers.

Every test here runs twice, once per store. The folder one runs always. The
Postgres one runs when there is a database to run against and skips otherwise,
so a checkout with no Docker still gets a green suite - the point of the
suite is that it needs nothing, and a test that silently vanishes is better
than one that fails on a laptop in a cafe.

    docker compose up -d && uv run alembic upgrade head

is what makes the second half of this file actually execute.
"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.domain.models import EmailAddress, NormalizedEmail
from src.infrastructure.blobs import FolderBlobs
from src.infrastructure.documents import SourceFile
from src.infrastructure.storage.database import DatabaseRecords
from src.infrastructure.storage.records import (
    DOWNLOAD_FAILED,
    EmailRecords,
    RecordedCandidate,
    RecordedDelivery,
    RecordedExtraction,
    RecordedMatch,
)

# A database of its own, and not the one `.env` points at. These tests truncate
# between cases, and truncating the database somebody is developing against is
# how an afternoon's records disappear.
#
#     docker compose exec db createdb -U rfq rfq_test
#     TEST_DATABASE_URL=... uv run alembic upgrade head
DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://rfq:rfq_local_dev@localhost:5433/rfq_test"
)


def _postgres_is_up() -> bool:
    """Whether there is a database on the other end, without a test touching it.

    Asked once, at import, because thirty tests each waiting on a connection
    timeout is thirty seconds nobody wants.
    """
    try:
        from sqlalchemy.ext.asyncio import create_async_engine

        async def probe() -> bool:
            engine = create_async_engine(DATABASE_URL, connect_args={"timeout": 2})
            try:
                async with engine.connect():
                    return True
            finally:
                await engine.dispose()

        return asyncio.run(probe())
    except Exception:
        return False


HAS_POSTGRES = _postgres_is_up()


def email(**overrides) -> NormalizedEmail:
    return NormalizedEmail(
        message_id=overrides.pop("message_id", "AAMk-contract-1"),
        mailbox="supply@sevenseas.example.com",
        sender=EmailAddress(name="Purchasing", address="purchasing@almi.example.com"),
        to=[EmailAddress(address="supply@sevenseas.example.com")],
        cc=[],
        subject="RFQ 88210 / MV WESTERN STAR / Jebel Ali",
        body_text="Please quote the attached.",
        received_at=datetime(2026, 9, 13, 17, 3, 3, tzinfo=UTC),
        attachments=[],
        **overrides,
    )


def source(name: str = "Requisition.xlsx", data: bytes | None = b"xlsx-bytes") -> SourceFile:
    return SourceFile(filename=name, data=data, content_type=None, size_bytes=len(data or b""))


EXTRACTION = RecordedExtraction(
    items=2,
    complete=True,
    warnings=["imo_check_digit_failed"],
    header={
        "vessel_name": {"raw": "MV WESTERN STAR", "value": "MV WESTERN STAR"},
        "imo": {"raw": "9256379", "value": "9256379"},
        "delivery_port": {"raw": "Jebel Ali", "value": "Jebel Ali"},
        "eta": {"raw": "08 Dec", "value": "2026-12-08"},
    },
)

MATCHING = [
    RecordedMatch(
        index=1,
        verbatim="Convex rulers",
        description="RULE CONVEX",
        customer_code="650823",
        quantity="6",
        uom="pcs",
        item_code="T65082300",
        item_description="RULE CONVEX STEEL METRIC 5MTR",
        item={"Item Code": "T65082300", "UOM": "PCS"},
        confidence=100,
        how="code_confirmed",
        why="Descriptions agree (100%).",
    ),
    RecordedMatch(
        index=2,
        verbatim="Fire hose 2.5 inch",
        description="FIRE HOSE 2.5 INCH",
        customer_code="851163",
        quantity="4",
        uom="pcs",
        how="code_rejected",
        why="Agrees only 0% - rejected.",
        candidates=[
            RecordedCandidate(item_code="T610000022", confidence=100, item={"a": "1"}),
            RecordedCandidate(item_code="T610000024", confidence=85, item={"a": "2"}),
        ],
    ),
]

DELIVERY = RecordedDelivery(
    outcome="SENT",
    forwarded_to="uae-desk@example.invalid",
    cc=["cc@example.invalid"],
    region="uae",
    region_rule="delivery_port",
    attached="88210.xlsx",
)


@pytest.fixture(params=["folder", "postgres"])
async def store(request, tmp_path: Path):
    """One store, of each kind, empty."""
    if request.param == "folder":
        yield EmailRecords(tmp_path / "Database", enabled=True)
        return

    if not HAS_POSTGRES:
        pytest.skip("no Postgres on " + DATABASE_URL)

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Every test starts on an empty database. Truncating rather than
        # dropping keeps the schema the migration made, which is the schema
        # this is supposed to be testing against.
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE emails CASCADE"))
        yield DatabaseRecords(sessions, FolderBlobs(tmp_path / "blobs"), enabled=True)
    finally:
        await engine.dispose()


async def test_a_record_opens_with_the_verdict_and_the_email(store):
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-1", source="outlook"
    )

    assert record_id
    found = await store.read(record_id)
    assert found is not None
    assert found.subject == "RFQ 88210 / MV WESTERN STAR / Jebel Ali"
    assert found.sender is not None and found.sender.address == "purchasing@almi.example.com"
    assert found.mailbox == "supply@sevenseas.example.com"
    assert found.body_chars == len("Please quote the attached.")
    assert found.decision_id == "b7d5d04d-1"


async def test_everything_added_later_lands_on_the_same_record(store):
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-2", source="outlook"
    )

    await store.update(
        record_id,
        files=[source()],
        labels=["SSG RFQ"],
        labelled=True,
        extraction=EXTRACTION,
        matching=MATCHING,
        delivery=DELIVERY,
        form=("88210.xlsx", b"form-bytes"),
    )

    found = await store.read(record_id)
    assert found is not None
    assert found.labels == ["SSG RFQ"] and found.labelled is True
    assert found.extraction is not None and found.extraction.items == 2
    assert found.extraction.header["eta"]["value"] == "2026-12-08"
    assert found.delivery is not None and found.delivery.outcome == "SENT"
    assert found.delivery.cc == ["cc@example.invalid"]
    assert found.form is not None and found.form.filename == "88210.xlsx"
    assert [one.filename for one in found.attachments] == ["Requisition.xlsx"]


async def test_the_lines_come_back_in_order_with_their_candidates(store):
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-3", source="outlook"
    )
    await store.update(record_id, matching=MATCHING)

    found = await store.read(record_id)
    assert found is not None
    assert [one.index for one in found.matching] == [1, 2]

    first, second = found.matching
    assert first.how == "code_confirmed" and first.item_code == "T65082300"
    assert first.item == {"Item Code": "T65082300", "UOM": "PCS"}
    assert first.candidates == []
    # The shortlist keeps the order the search ranked it in. Read off the
    # screen top to bottom, so a store that returns it by id is wrong.
    assert [one.item_code for one in second.candidates] == ["T610000022", "T610000024"]
    assert [one.confidence for one in second.candidates] == [100, 85]


async def test_a_file_comes_back_byte_for_byte(store):
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-4", source="outlook"
    )
    await store.update(record_id, files=[source()], form=("88210.xlsx", b"form-bytes"))

    found = await store.read(record_id)
    assert found is not None
    assert found.attachments[0].saved_as and found.form is not None

    assert await store.file(record_id, found.attachments[0].saved_as) == b"xlsx-bytes"
    assert await store.file(record_id, found.form.saved_as) == b"form-bytes"


async def test_a_file_with_no_bytes_is_recorded_with_the_reason(store):
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-5", source="outlook"
    )
    await store.update(record_id, files=[source(data=None)])

    found = await store.read(record_id)
    assert found is not None
    entry = found.attachments[0]
    # An empty cell and a missing file are different facts, and the record has
    # to say which this was.
    assert entry.saved_as is None
    assert entry.note == DOWNLOAD_FAILED


async def test_a_name_from_outside_the_record_is_not_served(store):
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-6", source="outlook"
    )
    await store.update(record_id, files=[source()])

    assert await store.file(record_id, "../../.env") is None
    assert await store.file(record_id, "nothing.xlsx") is None
    assert await store.file("no-such-record", "Requisition.xlsx") is None


async def test_our_form_cannot_overwrite_a_file_of_the_customer_s(store):
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-7", source="outlook"
    )
    await store.update(record_id, files=[source(name="88210.xlsx", data=b"theirs")])
    await store.update(record_id, form=("88210.xlsx", b"ours"))

    found = await store.read(record_id)
    assert found is not None
    assert found.form is not None and found.attachments[0].saved_as
    assert found.form.saved_as != found.attachments[0].saved_as
    assert await store.file(record_id, found.attachments[0].saved_as) == b"theirs"
    assert await store.file(record_id, found.form.saved_as) == b"ours"


async def test_records_come_back_newest_first(store):
    first = await store.open(
        email=email(message_id="one"), outcome=None, decision_id="aaaaaaaa", source="outlook"
    )
    second = await store.open(
        email=email(message_id="two"), outcome=None, decision_id="bbbbbbbb", source="outlook"
    )

    found = await store.all()
    assert {one.id for one in found} == {first, second}
    assert [one.id for one in found][0] == second


async def test_a_second_matching_run_replaces_the_first(store):
    """Matching answers about every line at once, so a rerun is a new answer."""
    record_id = await store.open(
        email=email(), outcome=None, decision_id="b7d5d04d-8", source="outlook"
    )
    await store.update(record_id, matching=MATCHING)
    await store.update(record_id, matching=MATCHING[:1])

    found = await store.read(record_id)
    assert found is not None
    assert [one.index for one in found.matching] == [1]


async def test_reading_something_that_is_not_there_is_not_an_error(store):
    assert await store.read("2026-01-01T00-00-00Z__deadbeef") is None
    assert await store.file("2026-01-01T00-00-00Z__deadbeef", "x.xlsx") is None
