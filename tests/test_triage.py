"""The shared use case, and the mailbox handler that sits on top of it.

The point of these tests is that both entry points behave identically: an email
arriving from Graph must be classified and journalled exactly like one posted to
the endpoint, only tagged with a different source.
"""

from pathlib import Path

import pytest

from src.domain.enums import Direction, EmailCategory, RecommendedAction
from src.domain.models import EmailAddress, NormalizedEmail
from src.domain.rules.registries import OutlookCategories, Registries
from src.infrastructure.outlook.schemas import EmailMessage
from src.infrastructure.llm.exceptions import LLMCallError
from src.infrastructure.storage.decisions import DecisionLog
from src.services.classification.pipeline import ClassificationPipeline
from src.services.classification.schemas import LLMClassification
from src.services.handlers import ClassifyingEmailHandler
from src.services.triage import EmailTriage
from tests.fakes import BrokenLLM, FakeLLM, fake_settings

REGISTRIES = Registries.load(
    Path("config/registries.yaml"), mailbox="supply@our-company.com"
)

ANSWER = LLMClassification(
    category=EmailCategory.NEW_RFQ,
    direction=Direction.INBOUND_CUSTOMER,
    is_rfq=True,
    confidence=0.95,
    reasoning="Customer asks the chandler to quote an attached RFQ form.",
)

GRAPH_MESSAGE = EmailMessage.model_validate(
    {
        "id": "AAMkAGNjYzI0NDU4",
        "subject": "VSL: NORTH STAR, QUOTATION: 0015-AB000001C",
        "from": {"emailAddress": {"address": "purchasing@new-company.com"}},
        "toRecipients": [{"emailAddress": {"address": "supply@our-company.com"}}],
        "body": {"contentType": "html", "content": "<p>you may find attached our RFQ</p>"},
    }
)


def build(tmp_path: Path, *, journalling: bool = True) -> tuple[EmailTriage, DecisionLog, FakeLLM]:
    settings = fake_settings()
    llm = FakeLLM(ANSWER)
    journal = DecisionLog(
        tmp_path / "decisions.jsonl", enabled=journalling, log_bodies=False
    )
    pipeline = ClassificationPipeline(llm, REGISTRIES, settings)
    return EmailTriage(pipeline, journal, settings.PROMPT_VERSION), journal, llm


class FakeMailbox:
    """Records the labels the handler tries to stamp, instead of calling Graph."""

    def __init__(self, address: str = "supply@our-company.com", fails: bool = False) -> None:
        self.address = address
        self.labels: list[list[str]] = []
        self._fails = fails

    async def set_categories(self, message_id: str, categories: list[str]) -> None:
        if self._fails:
            raise RuntimeError("Graph said no")
        self.labels.append(categories)


def handler(triage: EmailTriage, mailbox: FakeMailbox | None = None):
    box = mailbox or FakeMailbox()
    return ClassifyingEmailHandler(triage, box, REGISTRIES.outlook_categories), box


def http_email() -> NormalizedEmail:
    return NormalizedEmail(
        sender=EmailAddress(address="purchasing@new-company.com"),
        to=[EmailAddress(address="supply@our-company.com")],
        subject="VSL: NORTH STAR, QUOTATION: 0015-AB000001C",
        body_text="you may find attached our RFQ",
    )


# --------------------------------------------------------------------------- #
# The use case
# --------------------------------------------------------------------------- #


async def test_triage_classifies_and_journals_in_one_step(tmp_path: Path) -> None:
    triage, journal, _ = build(tmp_path)
    triaged = await triage.run(http_email(), source="http")

    entries = list(journal.records())
    assert triaged.outcome.result.category is EmailCategory.NEW_RFQ
    assert len(entries) == 1
    assert entries[0]["decision_id"] == triaged.decision_id


async def test_the_source_is_recorded_so_both_entries_stay_distinguishable(
    tmp_path: Path,
) -> None:
    triage, journal, _ = build(tmp_path)
    await triage.run(http_email(), source="http")
    await triage.run(http_email(), source="outlook")

    assert [entry["source"] for entry in journal.records()] == ["http", "outlook"]


async def test_no_journal_means_no_decision_id(tmp_path: Path) -> None:
    triage, _, _ = build(tmp_path, journalling=False)
    assert (await triage.run(http_email(), source="http")).decision_id is None


# --------------------------------------------------------------------------- #
# The mailbox handler
# --------------------------------------------------------------------------- #


async def test_an_email_from_graph_goes_through_the_same_triage(tmp_path: Path) -> None:
    triage, journal, llm = build(tmp_path)
    graph_handler, _ = handler(triage)

    await graph_handler.handle(GRAPH_MESSAGE)

    entry = next(journal.records())
    assert len(llm.calls) == 1
    assert entry["source"] == "outlook"
    assert entry["result"]["category"] == "NEW_RFQ"
    assert entry["result"]["recommended_action"] == "FORWARD_TO_DST"


async def test_the_handler_records_which_mailbox_the_email_came_from(tmp_path: Path) -> None:
    triage, journal, _ = build(tmp_path)
    graph_handler, _ = handler(triage)

    await graph_handler.handle(GRAPH_MESSAGE)

    assert next(journal.records())["email"]["mailbox"] == "supply@our-company.com"


async def test_graph_and_http_reach_the_same_verdict(tmp_path: Path) -> None:
    """The whole reason both entries share one pipeline."""
    triage, journal, _ = build(tmp_path)

    graph_handler, _ = handler(triage)
    await triage.run(http_email(), source="http")
    await graph_handler.handle(GRAPH_MESSAGE)

    over_http, over_graph = (entry["result"] for entry in journal.records())
    assert over_http["category"] == over_graph["category"]
    assert over_http["recommended_action"] == over_graph["recommended_action"]
    assert over_http["is_rfq"] == over_graph["is_rfq"]


# --------------------------------------------------------------------------- #
# Labelling the mailbox copy
# --------------------------------------------------------------------------- #


async def test_an_rfq_is_labelled_for_dst(tmp_path: Path) -> None:
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage)

    await graph_handler.handle(GRAPH_MESSAGE)

    assert box.labels == [["the chandler RFQ"]]


async def test_a_flagged_decision_gets_both_labels(tmp_path: Path) -> None:
    """The routing label says where it goes, the review label says look at it."""
    unsure = LLMClassification(
        category=EmailCategory.NEW_RFQ,
        direction=Direction.INBOUND_CUSTOMER,
        is_rfq=True,
        confidence=0.70,
        reasoning="Plausible but one signal is missing.",
    )
    settings = fake_settings()
    journal = DecisionLog(tmp_path / "d.jsonl", enabled=False, log_bodies=False)
    triage = EmailTriage(
        ClassificationPipeline(FakeLLM(unsure), REGISTRIES, settings),
        journal,
        settings.PROMPT_VERSION,
    )
    graph_handler, box = handler(triage)

    await graph_handler.handle(GRAPH_MESSAGE)

    assert box.labels == [["the chandler RFQ", "the chandler Review"]]


async def test_every_action_maps_to_a_label(tmp_path: Path) -> None:
    """No label at all means "never processed", so no action may be left unmapped."""
    mapped = REGISTRIES.outlook_categories.by_action
    assert set(mapped) == set(RecommendedAction)


async def test_a_failure_is_labelled_and_still_raised(tmp_path: Path) -> None:
    settings = fake_settings()
    triage = EmailTriage(
        ClassificationPipeline(BrokenLLM(LLMCallError("m", RuntimeError("boom"))), REGISTRIES, settings),
        DecisionLog(tmp_path / "d.jsonl", enabled=False, log_bodies=False),
        settings.PROMPT_VERSION,
    )
    graph_handler, box = handler(triage)

    with pytest.raises(LLMCallError):
        await graph_handler.handle(GRAPH_MESSAGE)

    assert box.labels == [["the chandler Error"]]


async def test_a_label_that_will_not_stick_does_not_lose_the_decision(tmp_path: Path) -> None:
    """Graph refusing the write must not undo a classification already journalled."""
    triage, journal, _ = build(tmp_path)
    graph_handler, _ = handler(triage, FakeMailbox(fails=True))

    await graph_handler.handle(GRAPH_MESSAGE)

    assert next(journal.records())["result"]["category"] == "NEW_RFQ"
