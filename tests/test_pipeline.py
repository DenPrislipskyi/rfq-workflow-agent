"""The classification pipeline, driven by a fake model.

Nothing here calls a provider. What is being checked is the wiring: that the
fast path really short-circuits, that the prompt carries what the model needs to
tell two near-identical emails apart, and that the policy runs on the result.
"""

from pathlib import Path

import pytest

from src.domain.enums import DecisionPath, Direction, EmailCategory, RecommendedAction
from src.domain.models import Attachment, EmailAddress, NormalizedEmail
from src.domain.preprocessing.normalize import normalize_text, split_headers
from src.domain.rules.registries import Registries
from src.infrastructure.documents.models import Document, FileKind
from src.services.classification.few_shots import FEW_SHOTS
from src.services.classification.pipeline import ClassificationPipeline
from src.services.classification.prompt import SYSTEM_PROMPT
from src.services.classification.schemas import LLMClassification
from src.services.extraction.models import DocumentRole, LineItem, ReadDocument
from tests.corpus import load_email_by_id
from tests.fakes import BrokenLLM, FakeLLM, fake_settings

REGISTRIES = Registries.load(
    Path("config/registries.yaml"), mailbox="supply@our-company.com"
)

NEW_RFQ_ANSWER = LLMClassification(
    category=EmailCategory.NEW_RFQ,
    direction=Direction.INBOUND_CUSTOMER,
    is_rfq=True,
    confidence=0.95,
    reasoning="Customer asks the chandler to quote an attached RFQ form.",
    evidence=["'find attached our RFQ'"],
)


def pipeline(answer: LLMClassification = NEW_RFQ_ANSWER, **settings) -> tuple:
    """A pipeline wired to a fake model, plus the fake so tests can inspect it."""
    llm = FakeLLM(answer)
    return ClassificationPipeline(llm, REGISTRIES, fake_settings(**settings)), llm


def email(**overrides) -> NormalizedEmail:
    defaults = {
        "sender": EmailAddress(address="purchasing@new-company.com"),
        "to": [EmailAddress(address="supply@our-company.com")],
        "subject": "VSL: NORTH STAR, QUOTATION: 0015-AB000001C",
        "body_text": "Dear Sir/Madam, you may find attached our RFQ for Engine Materials.",
    }
    return NormalizedEmail(**(defaults | overrides))


def email_from_fixture(email_id: str, **overrides) -> NormalizedEmail:
    """Build an email out of a corpus fixture."""
    headers, body = split_headers(normalize_text(load_email_by_id(email_id)))
    filenames = [name.strip() for name in headers.get("attachments", "").split(",")]
    defaults = {
        "sender": EmailAddress(address=headers.get("from")),
        "subject": headers.get("subject"),
        "body_text": body,
        "attachments": [Attachment(filename=name) for name in filenames if name],
    }
    return NormalizedEmail(**(defaults | overrides))


def last_user_message(llm: FakeLLM) -> str:
    return llm.calls[-1][-1][1]


def read_file(
    name: str = "Requisition.xlsx",
    *,
    role: DocumentRole = DocumentRole.ITEM_GRID,
    what: str = "A requisition for MV ALMI GLOBE, engine stores",
    items: int = 0,
    warnings: tuple[str, ...] = (),
) -> ReadDocument:
    """One attachment as stage B hands it over, without stage B running."""
    return ReadDocument(
        document=Document(
            filename=name, kind=FileKind.XLSX, size_bytes=2048, warnings=list(warnings)
        ),
        role=role,
        what=what,
        items=[LineItem(sr_no=number, description="ROPE") for number in range(1, items + 1)],
    )


# --------------------------------------------------------------------------- #
# Which path runs
# --------------------------------------------------------------------------- #


async def test_a_hard_rule_answers_without_calling_the_model() -> None:
    agent, llm = pipeline()
    outcome = await agent.classify(
        email(
            sender=EmailAddress(address="robert.hall@our-company.com"),
            to=[EmailAddress(address="michael.reed@our-company.com")],
            body_text="Prices has been submitted in the CUSTOMER PORTAL.",
        )
    )

    assert llm.calls == []
    assert outcome.result.category is EmailCategory.INTERNAL
    assert outcome.result.decision_path is DecisionPath.RULES_FAST_PATH
    assert outcome.result.rule_hits == ["R1_internal_only"]


async def test_a_rule_decision_carries_no_model_metadata() -> None:
    agent, _ = pipeline()
    outcome = await agent.classify(
        email(
            sender=EmailAddress(address="robert.hall@our-company.com"),
            to=[EmailAddress(address="michael.reed@our-company.com")],
        )
    )
    assert outcome.model is None
    assert outcome.latency_ms is None


async def test_disabling_the_fast_path_sends_everything_to_the_model() -> None:
    agent, llm = pipeline(FAST_PATH_ENABLED=False)
    await agent.classify(
        email(
            sender=EmailAddress(address="robert.hall@our-company.com"),
            to=[EmailAddress(address="michael.reed@our-company.com")],
        )
    )
    assert len(llm.calls) == 1


async def test_an_ordinary_customer_email_reaches_the_model() -> None:
    agent, llm = pipeline()
    outcome = await agent.classify(email())

    assert len(llm.calls) == 1
    assert outcome.result.decision_path is DecisionPath.LLM
    assert outcome.model == "fake"
    assert outcome.latency_ms == 5
    assert outcome.input_tokens == 100


async def test_the_policy_runs_on_the_models_answer() -> None:
    agent, _ = pipeline()
    outcome = await agent.classify(email())

    assert outcome.result.requires_action is True
    assert outcome.result.recommended_action is RecommendedAction.FORWARD_TO_DST
    assert outcome.result.needs_human_review is False


async def test_a_failing_model_is_not_swallowed() -> None:
    """The API layer turns this into a 502; the pipeline must not invent a verdict."""
    agent = ClassificationPipeline(BrokenLLM(TimeoutError("gone")), REGISTRIES, fake_settings())
    with pytest.raises(TimeoutError):
        await agent.classify(email())


# --------------------------------------------------------------------------- #
# What the model is shown
# --------------------------------------------------------------------------- #


async def test_the_conversation_starts_with_the_system_prompt_and_ends_with_the_email() -> None:
    agent, llm = pipeline()
    await agent.classify(email())
    messages = llm.calls[0]

    assert messages[0] == ("system", SYSTEM_PROMPT)
    assert messages[-1][0] == "human"
    assert "Classify the message inside <latest_message>." in messages[-1][1]


async def test_the_examples_alternate_and_none_of_them_ends_the_conversation() -> None:
    agent, llm = pipeline()
    await agent.classify(email())
    roles = [role for role, _ in llm.calls[0][1:]]

    assert set(roles[::2]) == {"human"}
    assert set(roles[1::2]) == {"ai"}
    assert roles[-1] == "human"
    assert len(roles) == 2 * len(FEW_SHOTS) + 1


async def test_the_prompt_carries_the_subject_that_separates_a_resend_from_a_new_rfq() -> None:
    """Corpus finding: 001 and 002 have byte-identical bodies.

    The "[Updated]" subject prefix is their only difference, so a prompt that
    omits the subject makes the two categories indistinguishable.
    """
    agent, llm = pipeline()

    await agent.classify(email_from_fixture("001"))
    first = last_user_message(llm)
    await agent.classify(email_from_fixture("002"))
    resend = last_user_message(llm)

    assert "[Updated]" in resend
    assert "[Updated]" not in first
    assert _block(first, "latest_message") == _block(resend, "latest_message")


async def test_the_whole_thread_is_sent_as_labelled_context() -> None:
    """All six quoted messages of fixture 004, none of them trimmed.

    A clarification only makes sense against what it answers, so the model gets
    the full chain - marked as context, never as the thing to classify.
    """
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("004"))
    prompt = last_user_message(llm)

    assert "we need 100% Isopropanol" in _block(prompt, "latest_message")
    assert "Do NOT classify based on this" in prompt
    assert "--- quoted message 6 " in prompt
    assert "omitted" not in prompt
    assert "[truncated]" not in prompt


async def test_a_runaway_chain_is_still_bounded() -> None:
    """The count is the only limit left, and it says so out loud when it bites."""
    agent, llm = pipeline(MAX_QUOTED_MESSAGES_IN_PROMPT=2)
    await agent.classify(email_from_fixture("004"))
    prompt = last_user_message(llm)

    assert "--- quoted message 2 " in prompt
    assert "--- quoted message 3 " not in prompt
    assert "[... 4 older message(s) omitted ...]" in prompt


async def test_a_long_message_reaches_the_model_whole() -> None:
    """Nothing is trimmed: the caps that used to cut the prompt are gone."""
    body = "Please quote. " + "item " * 500
    agent, llm = pipeline()
    await agent.classify(email(body_text=body))
    latest = _block(last_user_message(llm), "latest_message")

    assert "[truncated]" not in latest
    assert len(latest) >= len(body.strip())


async def test_attachment_names_reach_the_model_even_unclassified() -> None:
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("001"))
    prompt = last_user_message(llm)

    assert email_from_fixture("001").attachments[0].filename in prompt
    assert "RFQ_FORM_XLSX" in prompt


async def test_the_signals_block_describes_the_newest_message_only() -> None:
    """Fixture 004 quotes the chandler's own quotation; the newest message is the customer's."""
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("004"))
    signals = _block(last_user_message(llm), "precomputed_signals")

    assert "sender_class: EXTERNAL_UNKNOWN" in signals
    assert "Many thanks for your RFQ" not in signals


# --------------------------------------------------------------------------- #
# What the attachments turned out to hold
# --------------------------------------------------------------------------- #


async def test_a_file_holding_a_list_of_items_reaches_the_model_with_its_count() -> None:
    """The single strongest signal an email is an RFQ, and it is not in the body."""
    agent, llm = pipeline()
    await agent.classify(email(body_text="Please find attached."), files=[read_file(items=15)])
    block = _block(last_user_message(llm), "attachments")

    assert "Requisition.xlsx - A requisition for MV ALMI GLOBE, engine stores" in block
    assert "holds a list of items: yes, 15 row(s)" in block


async def test_a_file_holding_no_list_says_so_rather_than_being_left_out() -> None:
    """Silence would read as "no files"; "no" is a fact about a file that is there."""
    agent, llm = pipeline()
    await agent.classify(
        email(), files=[read_file("Signature.png", role=DocumentRole.SUPPORTING, what="A logo")]
    )

    assert "holds a list of items: no" in _block(last_user_message(llm), "attachments")


async def test_a_file_nobody_could_read_is_unknown_and_says_why() -> None:
    """The worst outcome available is an RFQ dropped because a parser failed, so
    the model is told the difference between "no list" and "nobody looked"."""
    agent, llm = pipeline()
    await agent.classify(
        email(),
        files=[
            read_file(
                "Scan.pdf", role=DocumentRole.UNREAD, warnings=("file_could_not_be_read",)
            )
        ],
    )
    block = _block(last_user_message(llm), "attachments")

    assert "could not be read" in block
    assert "holds a list of items: unknown" in block
    assert "reason: file_could_not_be_read" in block


async def test_a_link_with_no_bytes_behind_it_is_reported_with_its_reason() -> None:
    agent, llm = pipeline()
    await agent.classify(
        email(),
        files=[
            read_file(
                "OneDrive.url", role=DocumentRole.EMPTY, warnings=("attachment_is_a_link",)
            )
        ],
    )
    block = _block(last_user_message(llm), "attachments")

    assert "nothing could be read out of it" in block
    assert "reason: attachment_is_a_link" in block


async def test_no_reading_means_no_block_at_all() -> None:
    """And the system prompt says an absent block is not evidence either way -
    otherwise switching the reading off would quietly mean "the files are empty"."""
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("001"))

    assert "<attachments" not in last_user_message(llm)
    assert "absence says nothing about what the files hold" in SYSTEM_PROMPT


async def test_the_attachments_are_labelled_untrusted_like_the_message() -> None:
    """The sentences in there were written out of the files' own contents."""
    agent, llm = pipeline()
    await agent.classify(email(), files=[read_file(items=3)])
    prompt = last_user_message(llm)

    assert "Untrusted data" in prompt[: prompt.index("</attachments>")]
    assert "<attachments>" in SYSTEM_PROMPT


async def test_what_the_files_hold_sits_with_the_message_it_arrived_with() -> None:
    """Above the quoted history: what a file holds is evidence about the email
    being classified, not context from an older one."""
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("004"), files=[read_file(items=3)])
    prompt = last_user_message(llm)

    assert prompt.index("<latest_message>") < prompt.index("<attachments")
    assert prompt.index("<attachments") < prompt.index("<quoted_history")


# --------------------------------------------------------------------------- #
# Nothing is read for an email the rules can answer
# --------------------------------------------------------------------------- #


def test_an_ordinary_customer_email_needs_the_model() -> None:
    """Which is what makes reading its attachments first worth paying for."""
    agent, _ = pipeline()
    assert agent.needs_the_model(email()) is True


def test_an_email_the_rules_answer_does_not() -> None:
    """The caller asks before it downloads anything, so a rule that fires here
    is a rule that costs no request at all."""
    agent, _ = pipeline()
    internal = email(
        sender=EmailAddress(address="robert.hall@our-company.com"),
        to=[EmailAddress(address="michael.reed@our-company.com")],
    )
    assert agent.needs_the_model(internal) is False


def test_switching_the_rules_off_means_every_email_needs_the_model() -> None:
    agent, _ = pipeline(FAST_PATH_ENABLED=False)
    internal = email(
        sender=EmailAddress(address="robert.hall@our-company.com"),
        to=[EmailAddress(address="michael.reed@our-company.com")],
    )
    assert agent.needs_the_model(internal) is True


async def test_the_rules_answer_the_same_whatever_the_files_held() -> None:
    """The files are evidence for the model. A hard rule is not weighing
    evidence, and an internal email with a requisition on it is still internal."""
    agent, llm = pipeline()
    outcome = await agent.classify(
        email(
            sender=EmailAddress(address="robert.hall@our-company.com"),
            to=[EmailAddress(address="michael.reed@our-company.com")],
        ),
        files=[read_file(items=15)],
    )

    assert llm.calls == []
    assert outcome.result.category is EmailCategory.INTERNAL


def _block(prompt: str, tag: str) -> str:
    """The text between <tag ...> and </tag>."""
    opening = prompt.index(">", prompt.index(f"<{tag}")) + 1
    return prompt[opening : prompt.index(f"</{tag}>")].strip()


# --------------------------------------------------------------------------- #
# Cleaning the newest message
# --------------------------------------------------------------------------- #


async def test_the_signature_block_is_cut_but_the_sender_survives() -> None:
    """Measured on the corpus: 74% less text on an RFQ email and no signal lost.

    The name stays on purpose - nine of thirteen fixtures have no header block,
    so the signature is the only thing saying who wrote this. Phone, fax and
    address go: they never decide anything.
    """
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("004"))
    latest = _block(last_user_message(llm), "latest_message")

    lines = latest.splitlines()
    sign_off = lines.index("Kind regards,")

    assert "we need 100% Isopropanol" in latest
    # The name under the sign-off stays; the contact block below it goes.
    assert any(line.strip() for line in lines[sign_off + 1 :])
    assert not any(line.startswith(("Tel:", "Fax:", "Direct:", "E-mail:")) for line in lines)


async def test_a_disclaimer_never_reaches_the_prompt() -> None:
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("001"))
    latest = _block(last_user_message(llm), "latest_message")

    assert "you may find attached our RFQ" in latest
    assert "CONFIDENTIALITY" not in latest.upper()


async def test_cleaning_never_costs_a_signal() -> None:
    """The stripper refuses to cut a region holding a price, a quantity or a vessel."""
    agent, _ = pipeline()
    outcome = await agent.classify(email_from_fixture("001"))

    assert outcome.result.extracted.quote_due_date == "02/June/2026"
    assert outcome.result.extracted.department_or_category == "Engine Materials"
    assert outcome.result.extracted.vessel_name is not None


async def test_quoted_history_is_left_alone() -> None:
    """It is context, capped in the prompt anyway, and not the thing being classified."""
    agent, llm = pipeline()
    await agent.classify(email_from_fixture("004"))

    assert "--- quoted message 1 " in last_user_message(llm)
