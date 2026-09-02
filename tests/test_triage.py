"""The shared use case, and the mailbox handler that sits on top of it.

The point of these tests is that both entry points behave identically: an email
arriving from Graph must be classified and journalled exactly like one posted to
the endpoint, only tagged with a different source.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

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
    Path("config/registries.yaml"),
    mailbox="supply@our-company.com",
    region_mailboxes={"uae": "uae-desk@example.invalid", "sg": "sg-desk@example.invalid"},
    region_cc={"uae": ["uae-cc@example.invalid", "ops@example.invalid"], "sg": []},
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


class Sent(NamedTuple):
    to: str
    cc: list[str]
    comment: str = ""


class FakeMailbox:
    """Records what the handler tried to do, instead of calling Graph."""

    def __init__(
        self,
        address: str = "supply@our-company.com",
        fails: bool = False,
        forward_fails: bool = False,
    ) -> None:
        self.address = address
        self.labels: list[list[str]] = []
        self.forwards: list[Sent] = []
        self._fails = fails
        self._forward_fails = forward_fails

    async def set_categories(self, message_id: str, categories: list[str]) -> None:
        if self._fails:
            raise RuntimeError("Graph said no")
        self.labels.append(categories)

    async def forward(self, message_id: str, *, to: str, cc: Sequence[str] = ()) -> None:
        if self._forward_fails:
            raise RuntimeError("Graph said no")
        self.forwards.append(Sent(to, list(cc)))


def handler(
    triage: EmailTriage, mailbox: FakeMailbox | None = None, *, forward_enabled: bool = False
):
    box = mailbox or FakeMailbox()
    handler = ClassifyingEmailHandler(
        triage, box, REGISTRIES, forward_enabled=forward_enabled
    )
    return handler, box


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

    assert box.labels == [["SSG RFQ"]]


async def test_an_unsure_verdict_is_labelled_for_review_only(tmp_path: Path) -> None:
    """One label per email: waiting for a person is not the same as routed."""
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

    assert box.labels == [["SSG Review"]]


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

    assert box.labels == [["SSG Error"]]


async def test_a_label_that_will_not_stick_does_not_lose_the_decision(tmp_path: Path) -> None:
    """Graph refusing the write must not undo a classification already journalled."""
    triage, journal, _ = build(tmp_path)
    graph_handler, _ = handler(triage, FakeMailbox(fails=True))

    await graph_handler.handle(GRAPH_MESSAGE)

    assert next(journal.records())["result"]["category"] == "NEW_RFQ"


# --------------------------------------------------------------------------- #
# Forwarding an RFQ to its regional desk
# --------------------------------------------------------------------------- #


UAE_DESK = REGISTRIES.regions["uae"].forward_to
SG_DESK = REGISTRIES.regions["sg"].forward_to


def graph_message(body: str, *, categories: list[str] | None = None) -> EmailMessage:
    return EmailMessage.model_validate(
        {
            "id": "AAMkAGNjYzI0NDU4",
            "subject": "VSL: NORTH STAR, QUOTATION: 0015-AB000001C",
            "from": {"emailAddress": {"address": "purchasing@new-company.com"}},
            "toRecipients": [{"emailAddress": {"address": "supply@our-company.com"}}],
            "body": {"contentType": "text", "content": body},
            "categories": categories or [],
        }
    )


async def test_an_rfq_is_forwarded_to_the_regional_desk(tmp_path: Path) -> None:
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, m/t North Star - eta Fujairah"))

    assert [sent.to for sent in box.forwards] == [UAE_DESK]


async def test_the_other_region_reaches_the_other_desk(tmp_path: Path) -> None:
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, delivery Singapore on the 3rd"))

    assert [sent.to for sent in box.forwards] == [SG_DESK]


async def test_the_forward_carries_nothing_the_agent_wrote(tmp_path: Path) -> None:
    """The email is relayed untouched.

    An earlier version wrote the verdict and the model's own reasoning above the
    forward. That is free text generated per email, and it reached the desk and
    everyone copied on it, so it is gone.
    """
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, eta Fujairah"))

    assert box.forwards[0].comment == ""


async def test_nothing_is_sent_while_the_flag_is_off(tmp_path: Path) -> None:
    """The default. Labels still appear, so the agent can be watched safely."""
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage)

    await graph_handler.handle(graph_message("our RFQ, eta Fujairah"))

    assert box.forwards == []
    assert box.labels == [["SSG RFQ"]]


async def test_only_an_rfq_is_forwarded(tmp_path: Path) -> None:
    settings = fake_settings()
    spam = LLMClassification(
        category=EmailCategory.SPAM_MARKETING,
        direction=Direction.UNKNOWN,
        is_rfq=False,
        confidence=0.95,
        reasoning="A newsletter.",
    )
    triage = EmailTriage(
        ClassificationPipeline(FakeLLM(spam), REGISTRIES, settings),
        DecisionLog(tmp_path / "d.jsonl", enabled=False, log_bodies=False),
        settings.PROMPT_VERSION,
    )
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our newsletter from Dubai"))

    assert box.forwards == []


async def test_an_rfq_with_no_clear_region_is_flagged_instead_of_guessed(tmp_path: Path) -> None:
    """Nobody is emailed on a guess; a person decides which desk it belongs to."""
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("please quote, we need it soon"))

    assert box.forwards == []
    assert box.labels == [["SSG RFQ", "SSG Not Sent"]]


async def test_two_regions_at_once_is_flagged_not_sent_twice(tmp_path: Path) -> None:
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("RFQ: Dubai stock for Singapore delivery"))

    assert box.forwards == []
    assert box.labels == [["SSG RFQ", "SSG Not Sent"]]


async def test_a_forward_that_fails_is_flagged_not_lost(tmp_path: Path) -> None:
    """Graph refusing to send must surface as work for a person, not as silence."""
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(
        triage, FakeMailbox(forward_fails=True), forward_enabled=True
    )

    await graph_handler.handle(graph_message("our RFQ, eta Fujairah"))

    assert box.labels == [["SSG RFQ", "SSG Not Sent"]]


async def test_a_message_this_agent_already_labelled_is_left_alone(tmp_path: Path) -> None:
    """Graph re-sends notifications and the in-memory dedupe dies with a restart.

    The label on the message is the durable record that this one already ran,
    which is what stops a customer's RFQ being forwarded a second time.
    """
    triage, journal, llm = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(
        graph_message("our RFQ, eta Fujairah", categories=["SSG RFQ"])
    )

    assert box.forwards == []
    assert box.labels == []
    assert llm.calls == []
    assert list(journal.records()) == []


async def test_an_unrelated_category_does_not_block_the_agent(tmp_path: Path) -> None:
    """People put their own categories on mail; only ours mean "already handled"."""
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(
        graph_message("our RFQ, eta Fujairah", categories=["Follow up", "Blue category"])
    )

    assert [sent.to for sent in box.forwards] == [UAE_DESK]


async def test_a_region_with_no_address_configured_is_flagged_not_sent(tmp_path: Path) -> None:
    """A blank UAE_MAILBOX must stop the mail, not crash and not send nowhere."""
    unconfigured = Registries.load(
        Path("config/registries.yaml"),
        mailbox="supply@our-company.com",
        region_mailboxes={"sg": "sg-desk@example.invalid"},
    )
    triage, _, _ = build(tmp_path)
    box = FakeMailbox()
    graph_handler = ClassifyingEmailHandler(triage, box, unconfigured, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, eta Fujairah"))

    assert box.forwards == []
    assert box.labels == [["SSG RFQ", "SSG Not Sent"]]


@pytest.mark.parametrize("category", list(EmailCategory))
async def test_an_email_is_forwarded_if_and_only_if_it_is_labelled_ssg_rfq(
    tmp_path: Path, category: EmailCategory
) -> None:
    """The whole routing contract, one case per category.

    Nothing else in the mailbox is ever sent anywhere: SSG Review, SSG No action
    and SSG Error only put a label on the message. Forwarding is not a separate
    switch a future change could drift away from the label - the two conditions
    are the same condition.
    """
    settings = fake_settings()
    answer = LLMClassification(
        category=category,
        direction=Direction.UNKNOWN,
        is_rfq=category in {EmailCategory.NEW_RFQ, EmailCategory.UPDATED_RFQ},
        confidence=0.95,
        reasoning="Fixed answer for this case.",
    )
    triage = EmailTriage(
        ClassificationPipeline(FakeLLM(answer), REGISTRIES, settings),
        DecisionLog(tmp_path / "d.jsonl", enabled=False, log_bodies=False),
        settings.PROMPT_VERSION,
    )
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, eta Fujairah"))

    assert bool(box.forwards) == (box.labels == [["SSG RFQ"]])


# --------------------------------------------------------------------------- #
# Copying a regional desk's colleagues
# --------------------------------------------------------------------------- #


async def test_the_desks_copy_list_travels_with_the_forward(tmp_path: Path) -> None:
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, eta Fujairah"))

    assert box.forwards[0].cc == ["uae-cc@example.invalid", "ops@example.invalid"]


async def test_a_desk_with_nobody_to_copy_sends_an_empty_list(tmp_path: Path) -> None:
    """Blank SG_CC must forward without a copy, not fail and not reuse UAE's."""
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, delivery Singapore"))

    assert box.forwards[0].to == SG_DESK
    assert box.forwards[0].cc == []


async def test_the_copy_list_never_leaks_into_the_recipient(tmp_path: Path) -> None:
    """`to` is one desk. A copied colleague must not become an addressee."""
    triage, _, _ = build(tmp_path)
    graph_handler, box = handler(triage, forward_enabled=True)

    await graph_handler.handle(graph_message("our RFQ, eta Fujairah"))

    assert box.forwards[0].to == UAE_DESK
    assert UAE_DESK not in box.forwards[0].cc
