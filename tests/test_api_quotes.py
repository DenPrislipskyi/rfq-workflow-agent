"""The endpoint the Quote Overview page reads.

The contract under test is not "does it return rows". It is that a record holds
the customer's own email, the model's reasoning and every file they attached,
and that **none of that** is on the wire: the page gets the label, the status
and who wrote in, and a field nobody put there on purpose is a bug.
"""

import asyncio
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import register_exceptions, register_routers
from src.api import quotes
from src.api.dependencies import get_changes, get_records
from src.domain.enums import (
    DecisionPath,
    DeliveryOutcome,
    Direction,
    EmailCategory,
    Priority,
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
    RecordedCandidate,
    RecordedDelivery,
    RecordedMatch,
)

URL = "/api/v1/quotes"
BODY = "Dear Sir/Madam, you may find attached our RFQ for Engine Materials."


def client(records: EmailRecords, changes: Changes | None = None) -> TestClient:
    """The app without its lifespan: no Graph subscription, no `.env`."""
    signal = changes or Changes()
    app = FastAPI()
    register_exceptions(app)
    register_routers(app)
    app.dependency_overrides[get_records] = lambda: records
    app.dependency_overrides[get_changes] = lambda: signal
    return TestClient(app)


def email(sender: str = "purchasing@new-company.com", **overrides) -> NormalizedEmail:
    return NormalizedEmail(
        message_id="AAMkAGI2",
        mailbox="supply@our-company.com",
        sender=EmailAddress(address=sender),
        subject="VSL: NORTH STAR",
        body_text=BODY,
        attachments=[Attachment(filename="Requisition.xlsx")],
        **overrides,
    )


def outcome(
    category: EmailCategory = EmailCategory.NEW_RFQ,
    action: RecommendedAction = RecommendedAction.FORWARD_TO_DST,
    priority: Priority = Priority.NORMAL,
) -> ClassificationOutcome:
    return ClassificationOutcome(
        result=ClassificationResult(
            category=category,
            direction=Direction.INBOUND_CUSTOMER,
            requires_action=True,
            is_rfq=category is EmailCategory.NEW_RFQ,
            recommended_action=action,
            confidence=0.95,
            needs_human_review=False,
            priority=priority,
            decision_path=DecisionPath.LLM,
            reasoning="The customer asks the chandler to quote an attached requisition.",
            evidence=["'find attached our RFQ'"],
            extracted=Signals(vessel_name="NORTH STAR"),
        ),
        thread=SplitThread(latest_message=BODY),
        hints=Hints(),
        model="gpt-5.6-luna",
    )


async def one_rfq(tmp_path: Path, **kwargs) -> tuple[EmailRecords, str]:
    """A recorded email, labelled and forwarded, as a finished run leaves it."""
    records = EmailRecords(tmp_path / "Database", enabled=True)
    record_id = await records.open(
        email=email(), outcome=outcome(**kwargs), decision_id="be34a9b8-e2d3", source="outlook"
    )
    assert record_id is not None
    await records.update(
        record_id,
        labels=["SSG RFQ"],
        labelled=True,
        delivery=RecordedDelivery(outcome=DeliveryOutcome.SENT.value, forwarded_to="uae@desk"),
        files=[SourceFile(filename="Requisition.xlsx", data=b"rows", size_bytes=4)],
    )
    return records, record_id


async def test_one_row_per_email_with_the_label_and_the_sender(tmp_path: Path):
    records, record_id = await one_rfq(tmp_path)

    body = client(records).get(URL).json()

    assert body["total"] == 1
    row = body["items"][0]
    assert row["labels"] == ["SSG RFQ"]
    assert row["customerName"] == "purchasing@new-company.com"
    assert row["rowKey"] == record_id


async def test_the_customer_s_own_words_never_reach_the_page(tmp_path: Path):
    """The record holds the body, the reasoning and the evidence. The row is
    what a browser gets, and it must carry none of them."""
    records, _ = await one_rfq(tmp_path)

    row = client(records).get(URL).json()["items"][0]

    printed = str(row)
    assert BODY not in printed
    assert "requisition" not in printed.lower()
    assert "subject" not in row and "reasoning" not in row


async def test_every_column_the_agent_cannot_answer_is_empty(tmp_path: Path):
    """Not zero, and not a plausible-looking guess: a table that reads as
    finished work is worse than one with blanks in it."""
    records, _ = await one_rfq(tmp_path)

    row = client(records).get(URL).json()["items"][0]

    assert row["counts"] is None
    assert row["completionPercent"] is None
    assert row["quotationNumber"] == ""
    assert row["responsibleUser"] == ""
    assert row["processingTime"] == ""
    assert row["receivedOn"] == ""
    # But it can be opened: every row on this list is an RFQ the agent read.
    assert row["id"] == row["rowKey"]


async def test_the_list_is_rfqs_and_not_the_rest_of_the_mailbox(tmp_path: Path):
    """The agent classifies everything that arrives; this page is about
    quoting. A supplier's reply and a marketing blast belong in the record
    folder and on the journal line, not here."""
    records = EmailRecords(tmp_path / "Database", enabled=True)
    forwarded = await records.open(
        email=email(), outcome=outcome(), decision_id="aaaa-1", source="outlook"
    )
    await records.update(
        forwarded,
        labels=["SSG RFQ"],
        delivery=RecordedDelivery(outcome=DeliveryOutcome.SENT.value),
    )
    await records.open(
        email=email("noreply@marketing.com"),
        outcome=outcome(EmailCategory.SPAM_MARKETING, RecommendedAction.IGNORE),
        decision_id="bbbb-2",
        source="outlook",
    )

    body = client(records).get(URL).json()

    assert body["total"] == 1
    assert body["items"][0]["customerName"] == "purchasing@new-company.com"
    assert body["items"][0]["status"] == "inProgress"


async def test_an_urgent_email_carries_its_priority(tmp_path: Path):
    records, _ = await one_rfq(tmp_path, priority=Priority.URGENT)

    row = client(records).get(URL).json()["items"][0]

    assert row["priority"] == "HIGH"


async def test_search_matches_the_two_columns_that_have_anything_in_them(
    tmp_path: Path,
):
    records = EmailRecords(tmp_path / "Database", enabled=True)
    for index, sender in enumerate(["almi@almi.gr", "noreply@portal.com"]):
        record_id = await records.open(
            email=email(sender), outcome=outcome(), decision_id=f"cccc-{index}", source="outlook"
        )
        await records.update(record_id, labels=["SSG RFQ" if index == 0 else "SSG No action"])

    found = client(records).get(URL, params={"search": "almi"}).json()

    assert found["total"] == 1
    assert found["items"][0]["customerName"] == "almi@almi.gr"


async def test_pages_are_pages(tmp_path: Path):
    records = EmailRecords(tmp_path / "Database", enabled=True)
    for index in range(3):
        await records.open(
            email=email(f"customer{index}@sea.com"),
            outcome=outcome(),
            decision_id=f"dddd-{index}",
            source="outlook",
        )

    body = client(records).get(URL, params={"page": 2, "pageSize": 2}).json()

    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["page"] == 2 and body["pageSize"] == 2


async def test_an_empty_database_is_an_empty_table_not_an_error(tmp_path: Path):
    records = EmailRecords(tmp_path / "Database", enabled=True)

    response = client(records).get(URL)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "pageSize": 25}


async def test_an_rfq_reads_as_its_lines(tmp_path: Path):
    """The screen compares two halves of each line - what the customer asked
    for, and what we sell - so the wire keeps them apart the way the page does."""
    records, record_id = await one_rfq(tmp_path)
    await records.update(
        record_id,
        matching=[
            RecordedMatch(
                index=1,
                verbatim="Hexagon Head Bolts (Bolt with Nut) M16*65",
                description="HEX HEAD BOLT NUT M16 X 65MM",
                customer_code="691284",
                quantity="500",
                uom="set",
                item_code="T69128400",
                item_description="HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM",
                confidence=98,
                item={"Item Code": "T69128400", "UOM": "SET", "Price": "0.42"},
                how="code_confirmed",
                why="Same bolt, same size.",
                candidates=[
                    RecordedCandidate(item_code="T69128400", description="HEX...", confidence=98),
                    RecordedCandidate(item_code="T69133100", description="HEX...", confidence=20),
                ],
            )
        ],
    )

    body = client(records).get(f"{URL}/{record_id}/rfq").json()

    assert body["customerName"] == "purchasing@new-company.com"
    assert body["vesselName"] == ""  # this record carries no extraction
    line = body["lines"][0]
    assert line["line"] == 1
    assert (line["customerCode"], line["quantity"], line["uom"]) == ("691284", "500", "set")
    assert line["customerDescription"] == "Hexagon Head Bolts (Bolt with Nut) M16*65"
    assert line["itemCode"] == "T69128400"
    assert line["confidence"] == 98
    # Every candidate, scored - a refusal only means something beside what it
    # refused, and this is what the review panel reads.
    assert [one["confidence"] for one in line["candidates"]] == [98, 20]
    assert line["item"]["Price"] == "0.42"


async def test_an_rfq_nobody_has_is_a_404(tmp_path: Path):
    records, _ = await one_rfq(tmp_path)

    assert client(records).get(f"{URL}/nothing-here/rfq").status_code == 404


async def test_one_email_can_be_read_in_full(tmp_path: Path):
    """The row is deliberately thin; this is where the whole record lives."""
    records, record_id = await one_rfq(tmp_path)

    body = client(records).get(f"{URL}/{record_id}").json()

    assert body["subject"] == "VSL: NORTH STAR"
    assert body["verdict"]["category"] == "NEW_RFQ"
    # The record goes out as it is on disk - this is the file itself, not a
    # view of it, and the folder is meant to be readable by a person.
    assert body["attachments"][0]["saved_as"] == "Requisition.xlsx"


async def test_an_attachment_comes_back_as_the_bytes_that_arrived(tmp_path: Path):
    records, record_id = await one_rfq(tmp_path)

    response = client(records).get(f"{URL}/{record_id}/files/Requisition.xlsx")

    assert response.status_code == 200
    assert response.content == b"rows"


async def test_the_stream_says_when_the_list_changed():
    """The page is told to read the list again; it is never told what to think.

    Driven through the generator rather than over HTTP: the response never
    ends, and a test client reading it to the end waits forever.
    """
    changes = Changes()
    stream = quotes.events(changes)
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # let it subscribe before anything is announced

    changes.announce()

    assert await asyncio.wait_for(waiting, timeout=1) == quotes.CHANGED
    await stream.aclose()


async def test_a_silent_stream_still_says_something(monkeypatch):
    """A connection that sends nothing for minutes is closed by every proxy
    between the page and the agent."""
    monkeypatch.setattr(quotes, "KEEPALIVE_SECONDS", 0.01)
    stream = quotes.events(Changes())

    assert await asyncio.wait_for(anext(stream), timeout=1) == quotes.KEEPALIVE
    await stream.aclose()


async def test_a_page_that_goes_away_stops_being_a_listener():
    """A closed browser tab must not leave an event behind to be set forever."""
    changes = Changes()
    stream = quotes.events(changes)
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # let it subscribe
    assert changes.listeners == 1

    # What a disconnected client does to the response body.
    waiting.cancel()
    with suppress(asyncio.CancelledError):
        await waiting

    assert changes.listeners == 0


async def test_a_path_out_of_the_record_is_a_404(tmp_path: Path):
    records, record_id = await one_rfq(tmp_path)

    assert client(records).get(f"{URL}/{record_id}/files/../../../.env").status_code == 404
    assert client(records).get(f"{URL}/nothing-here").status_code == 404
