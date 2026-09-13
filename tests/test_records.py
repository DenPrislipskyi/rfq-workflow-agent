"""The `Database/` folder: one record per email.

Two things are worth a test here and the happy path is neither of them. A
customer names their own attachments, and that name becomes a path on our
disk - so the interesting cases are the ones where the name is a weapon or a
collision. The other is that a record survives being written twice: the verdict
is known long before the delivery is, and a page reading the folder in between
must find a whole file either way.
"""

import json
from datetime import datetime
from pathlib import Path

from src.domain.enums import (
    DecisionPath,
    DeliveryOutcome,
    Direction,
    EmailCategory,
    RecommendedAction,
)
from src.domain.models import (
    Attachment,
    ClassificationOutcome,
    ClassificationResult,
    EmailAddress,
    Hints,
    NormalizedEmail,
    Signals,
    SplitThread,
)
from src.infrastructure.documents.loader import SourceFile
from src.infrastructure.storage.changes import Changes
from src.infrastructure.storage.records import (
    EmailRecords,
    RecordedDelivery,
    RecordedExtraction,
)

BODY = "Dear Sir/Madam, you may find attached our RFQ for Engine Materials."


def email() -> NormalizedEmail:
    return NormalizedEmail(
        message_id="AAMkAGI2",
        mailbox="supply@ourcompany.example.com",
        sender=EmailAddress(name="Purchasing", address="purchasing@newcompany.example.com"),
        subject="VSL: NORTH STAR",
        body_text=BODY,
        attachments=[Attachment(filename="Requisition.xlsx", size_bytes=2048)],
    )


def outcome() -> ClassificationOutcome:
    return ClassificationOutcome(
        result=ClassificationResult(
            category=EmailCategory.NEW_RFQ,
            direction=Direction.INBOUND_CUSTOMER,
            requires_action=True,
            is_rfq=True,
            recommended_action=RecommendedAction.FORWARD_TO_DST,
            confidence=0.95,
            needs_human_review=False,
            decision_path=DecisionPath.LLM,
            reasoning="Customer asks the chandler to quote.",
            extracted=Signals(vessel_name="NORTH STAR"),
        ),
        thread=SplitThread(latest_message=BODY),
        hints=Hints(),
        model="gpt-5.6-luna",
    )


def store(tmp_path: Path, **kwargs) -> EmailRecords:
    return EmailRecords(tmp_path / "Database", enabled=True, **kwargs)


async def opened(records: EmailRecords) -> str:
    record_id = await records.open(
        email=email(), outcome=outcome(), decision_id="be34a9b8-e2d3-469b", source="outlook"
    )
    assert record_id is not None
    return record_id


async def test_a_record_is_one_folder_with_the_email_in_it(tmp_path: Path):
    records = store(tmp_path)

    record_id = await opened(records)

    folder = tmp_path / "Database" / record_id
    assert (folder / "body.txt").read_text(encoding="utf-8") == BODY
    written = json.loads((folder / "email.json").read_text(encoding="utf-8"))
    assert written["subject"] == "VSL: NORTH STAR"
    assert written["sender"]["address"] == "purchasing@newcompany.example.com"
    assert written["verdict"]["category"] == "NEW_RFQ"


async def test_the_id_sorts_by_time_and_names_the_decision(tmp_path: Path):
    """A folder listing is a run in order, and pairs with the journal by eye."""
    record_id = await opened(store(tmp_path))

    stamp, _, tail = record_id.partition("__")
    assert tail == "be34a9b8"
    assert stamp.endswith("Z") and stamp[4] == "-"


async def test_an_attachment_is_kept_exactly_as_it_arrived(tmp_path: Path):
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        files=[SourceFile(filename="Requisition.xlsx", data=b"PK\x03\x04 rows", size_bytes=9)],
    )

    record = records.read(record_id)
    assert record is not None
    kept = record.attachments[0]
    assert kept.saved_as == "Requisition.xlsx"
    assert (tmp_path / "Database" / record_id / kept.saved_as).read_bytes() == b"PK\x03\x04 rows"


async def test_a_filename_cannot_write_outside_its_own_folder(tmp_path: Path):
    """The customer names the file. `../` in it is input, not an instruction."""
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        files=[SourceFile(filename="../../../.env", data=b"stolen", size_bytes=6)],
    )

    record = records.read(record_id)
    assert record is not None
    # The last segment, and not a dot-file either: what arrives is written
    # somewhere visible inside this record's own folder, or not at all.
    assert record.attachments[0].saved_as == "env"
    assert not (tmp_path / ".env").exists()
    assert (tmp_path / "Database" / record_id / "env").read_bytes() == b"stolen"


async def test_two_files_with_one_name_do_not_overwrite_each_other(tmp_path: Path):
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        files=[
            SourceFile(filename="rfq.xlsx", data=b"first", size_bytes=5),
            SourceFile(filename="rfq.xlsx", data=b"second", size_bytes=6),
        ],
    )

    record = records.read(record_id)
    assert record is not None
    assert [item.saved_as for item in record.attachments] == ["rfq.xlsx", "rfq (2).xlsx"]


async def test_a_file_with_no_bytes_is_recorded_with_the_reason(tmp_path: Path):
    """A link, a failed download and a file we chose not to keep are three
    different answers to "where is it?", and an empty folder is none of them."""
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        files=[
            SourceFile(filename="OneDrive.url", data=None, is_reference=True),
            SourceFile(filename="lost.xlsx", data=None),
        ],
    )

    record = records.read(record_id)
    assert record is not None
    assert [item.saved_as for item in record.attachments] == [None, None]
    assert all(item.note for item in record.attachments)


async def test_an_email_nobody_downloaded_says_so(tmp_path: Path):
    """The rules answer some emails without opening a thing. The record has to
    name the files anyway - the reviewer must see attachment 2 existed."""
    records = store(tmp_path)
    record_id = await opened(records)

    record = records.read(record_id)
    assert record is not None
    assert [item.filename for item in record.attachments] == ["Requisition.xlsx"]
    assert record.attachments[0].saved_as is None
    assert "rules" in (record.attachments[0].note or "")


async def test_keeping_attachments_off_leaves_the_record_and_drops_the_bytes(tmp_path: Path):
    records = store(tmp_path, keep_attachments=False)
    record_id = await opened(records)

    await records.update(
        record_id, files=[SourceFile(filename="rfq.xlsx", data=b"rows", size_bytes=4)]
    )

    record = records.read(record_id)
    assert record is not None
    assert record.attachments[0].size_bytes == 4
    assert record.attachments[0].saved_as is None
    assert not (tmp_path / "Database" / record_id / "rfq.xlsx").exists()


async def test_what_became_of_the_email_lands_on_the_same_record(tmp_path: Path):
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        labels=["SSG RFQ"],
        labelled=True,
        extraction=RecordedExtraction(items=15, complete=False, missing_required=["IMO"]),
        delivery=RecordedDelivery(
            outcome=DeliveryOutcome.SENT.value, forwarded_to="uae-desk@example.invalid", region="uae"
        ),
        form=("KASS_RFQ_NORTH_STAR.xlsx", b"PK\x03\x04 workbook"),
    )

    record = records.read(record_id)
    assert record is not None
    assert record.labels == ["SSG RFQ"] and record.labelled is True
    assert record.extraction is not None and record.extraction.items == 15
    assert record.delivery is not None and record.delivery.forwarded_to == "uae-desk@example.invalid"
    assert record.form is not None
    assert record.form.saved_as == "KASS_RFQ_NORTH_STAR.xlsx"
    assert (tmp_path / "Database" / record_id / record.form.saved_as).exists()


async def test_one_folder_holds_the_email_and_its_files_side_by_side(tmp_path: Path):
    """The layout is flat on purpose: opening a record shows the email and
    everything that came with it, with nothing to descend into."""
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        files=[SourceFile(filename="Requisition.xlsx", data=b"rows", size_bytes=4)],
        form=("KASS_RFQ_NORTH_STAR.xlsx", b"PK\x03\x04 workbook"),
    )

    folder = tmp_path / "Database" / record_id
    assert sorted(path.name for path in folder.iterdir()) == [
        "KASS_RFQ_NORTH_STAR.xlsx",
        "Requisition.xlsx",
        "body.txt",
        "email.json",
    ]
    assert all(path.is_file() for path in folder.iterdir())


async def test_an_attachment_cannot_take_a_name_of_ours(tmp_path: Path):
    """`email.json` is the record. A file the customer named the same thing
    goes in beside it, not over it."""
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        files=[
            SourceFile(filename="email.json", data=b"theirs", size_bytes=6),
            SourceFile(filename="body.txt", data=b"theirs", size_bytes=6),
        ],
    )

    record = records.read(record_id)
    assert record is not None
    assert [item.saved_as for item in record.attachments] == ["email (2).json", "body (2).txt"]
    assert record.subject == "VSL: NORTH STAR"
    assert (tmp_path / "Database" / record_id / "body.txt").read_text() == BODY


async def test_our_form_cannot_overwrite_a_file_of_the_customer_s(tmp_path: Path):
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(
        record_id,
        files=[SourceFile(filename="rfq.xlsx", data=b"theirs", size_bytes=6)],
        form=("rfq.xlsx", b"ours"),
    )

    record = records.read(record_id)
    assert record is not None
    assert record.form is not None and record.form.saved_as == "rfq (2).xlsx"
    assert (tmp_path / "Database" / record_id / "rfq.xlsx").read_bytes() == b"theirs"


async def test_the_verdict_survives_every_later_write(tmp_path: Path):
    """`update` rewrites the file. Losing the verdict to a delivery would be
    the worst kind of bug here: silent, and only visible days later."""
    records = store(tmp_path)
    record_id = await opened(records)

    await records.update(record_id, labels=["SSG RFQ"])
    await records.update(record_id, delivery=RecordedDelivery(outcome="SENT"))

    record = records.read(record_id)
    assert record is not None
    assert record.verdict is not None
    assert record.verdict.category == "NEW_RFQ"
    assert record.labels == ["SSG RFQ"]


async def test_a_record_is_never_half_written(tmp_path: Path):
    """Every write goes through a temporary file and one rename, so a page
    reading the folder mid-run finds a whole record or the previous one - and
    the temporary file is never left behind to be read as a record itself."""
    records = store(tmp_path)
    record_id = await opened(records)
    await records.update(record_id, labels=["SSG Review"])

    folder = tmp_path / "Database" / record_id
    assert sorted(path.name for path in folder.iterdir()) == ["body.txt", "email.json"]


async def test_recording_switched_off_writes_nothing_and_breaks_nothing(tmp_path: Path):
    records = EmailRecords(tmp_path / "Database", enabled=False)

    record_id = await records.open(
        email=email(), outcome=outcome(), decision_id=None, source="http"
    )
    await records.update(record_id, labels=["SSG RFQ"])

    assert record_id is None
    assert not (tmp_path / "Database").exists()


async def test_every_write_says_the_list_changed(tmp_path: Path):
    """A page watching the list is woken by the write itself, not by a clock."""
    changes = Changes()
    records = EmailRecords(tmp_path / "Database", enabled=True, changes=changes)

    with changes.subscribe() as changed:
        assert not changed.is_set()

        record_id = await opened(records)
        assert changed.is_set()

        changed.clear()
        await records.update(record_id, labels=["SSG RFQ"])
        assert changed.is_set()


async def test_nothing_is_announced_when_recording_is_off(tmp_path: Path):
    changes = Changes()
    records = EmailRecords(tmp_path / "Database", enabled=False, changes=changes)

    with changes.subscribe() as changed:
        await records.open(
            email=email(), outcome=outcome(), decision_id=None, source="http"
        )

        assert not changed.is_set()


async def test_records_come_back_newest_first(tmp_path: Path):
    records = store(tmp_path)
    first = await records.open(
        email=email().model_copy(update={"received_at": _at("2026-09-01T08:00:00Z")}),
        outcome=outcome(),
        decision_id="aaaaaaaa-1111",
        source="outlook",
    )
    second = await records.open(
        email=email().model_copy(update={"received_at": _at("2026-09-02T08:00:00Z")}),
        outcome=outcome(),
        decision_id="bbbbbbbb-2222",
        source="outlook",
    )

    assert [item.id for item in records.all()] == [second, first]


async def test_a_file_is_only_served_from_inside_its_own_record(tmp_path: Path):
    records = store(tmp_path)
    record_id = await opened(records)
    (tmp_path / "secret.txt").write_text("not yours")

    assert records.file(record_id, "body.txt") is not None
    assert records.file(record_id, "../../secret.txt") is None
    assert records.file("../../", "secret.txt") is None
    assert records.file(record_id, "attachments/nothing.xlsx") is None


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)
