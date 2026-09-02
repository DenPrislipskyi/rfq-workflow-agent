"""Registries, hard rules and hints.

What is left after the registry lists were measured to change nothing: who
counts as us, and the two rules that answer without the model.
"""

from pathlib import Path

import pytest

from src.domain.enums import (
    AttachmentKind,
    DecisionPath,
    Direction,
    EmailCategory,
    RecommendedAction,
    SenderClass,
)
from src.domain.models import (
    Attachment,
    ClassificationResult,
    EmailAddress,
    NormalizedEmail,
    SplitThread,
)
from src.domain.rules.fast_path import match_fast_path
from src.domain.rules.hints import build_hints, render_hints
from src.domain.rules.registries import Registries

REGISTRIES_PATH = Path("config/registries.yaml")
MAILBOX = "supply@our-company.com"
REGISTRIES = Registries.load(REGISTRIES_PATH, mailbox=MAILBOX)


def email(**overrides) -> NormalizedEmail:
    """A minimal customer email; override only what the test is about."""
    defaults = {
        "sender": EmailAddress(address="purchasing@new-company.com"),
        "to": [EmailAddress(address=MAILBOX)],
        "subject": "VSL: NORTH STAR, QUOTATION: 0015-AB000001C",
    }
    return NormalizedEmail(**(defaults | overrides))


def thread(text: str = "Please find attached our RFQ for Engine Materials.") -> SplitThread:
    return SplitThread(latest_message=text)


# --------------------------------------------------------------------------- #
# Who counts as us
# --------------------------------------------------------------------------- #


def test_the_watched_mailbox_domain_becomes_our_own() -> None:
    """Nobody writes it down twice: the address is already in Settings."""
    assert REGISTRIES.internal_domains == ["our-company.com"]


def test_a_different_deployment_gets_a_different_domain() -> None:
    other = Registries.load(REGISTRIES_PATH, mailbox="triage@other-company.com")
    assert other.internal_domains == ["other-company.com"]


def test_without_a_mailbox_nobody_is_internal() -> None:
    """Tests and the eval harness run this way; the fast path then simply declines."""
    assert Registries.load(REGISTRIES_PATH).internal_domains == []


def test_domains_are_matched_case_insensitively() -> None:
    assert REGISTRIES.is_internal_domain("Our-Company.com")


# --------------------------------------------------------------------------- #
# Outlook label names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", list(RecommendedAction))
@pytest.mark.parametrize("needs_review", [False, True])
def test_a_decision_earns_exactly_one_label(action: RecommendedAction, needs_review: bool) -> None:
    """Two labels would be two answers to "what happened to this email?".

    The single pair, SSG RFQ beside SSG Not Sent, is added by the handler when a
    forward does not happen - never by the category map itself.
    """
    result = ClassificationResult(
        category=EmailCategory.NEW_RFQ,
        direction=Direction.UNKNOWN,
        requires_action=True,
        is_rfq=True,
        recommended_action=action,
        confidence=0.9,
        needs_human_review=needs_review,
        decision_path=DecisionPath.LLM,
    )

    assert len(REGISTRIES.outlook_categories.for_result(result)) == 1


def test_an_unsent_rfq_is_the_only_email_with_two_labels() -> None:
    labels = REGISTRIES.outlook_categories
    rfq = labels.by_action[RecommendedAction.FORWARD_TO_DST]

    assert labels.plus_not_sent([rfq]) == ["SSG RFQ", "SSG Not Sent"]


def test_every_label_matches_a_category_that_exists_in_the_mailbox() -> None:
    """An operator creates these four by hand, so a typo here shows up as a
    colourless label rather than as an error."""
    labels = REGISTRIES.outlook_categories
    names = labels.all_names()

    assert names == {"SSG RFQ", "SSG Review", "SSG No action", "SSG Not Sent", "SSG Error"}


SENDERS = [
    ("michael.reed@our-company.com", SenderClass.INTERNAL_OWN),
    ("purchasing@new-company.com", SenderClass.EXTERNAL_UNKNOWN),
    ("sales@technical-company.com", SenderClass.EXTERNAL_UNKNOWN),
]


@pytest.mark.parametrize(("address", "expected"), SENDERS, ids=[a for a, _ in SENDERS])
def test_sender_classification(address: str, expected: SenderClass) -> None:
    """Everyone outside our own domain is simply external.

    Telling a customer from a supplier is the model's job; a directory of
    domains would never be complete anyway.
    """
    assert REGISTRIES.classify_sender(address, address.split("@")[1]) is expected


def test_missing_sender_is_unknown_not_external() -> None:
    """Raw dumps often have no header. Absence of data is not evidence."""
    assert REGISTRIES.classify_sender(None, None) is SenderClass.UNKNOWN


# --------------------------------------------------------------------------- #
# The two rules that are left
# --------------------------------------------------------------------------- #


def test_r1_internal_conversation() -> None:
    decision = match_fast_path(
        email(
            sender=EmailAddress(address="robert.hall@our-company.com"),
            to=[EmailAddress(address="michael.reed@our-company.com")],
        ),
        thread("The RFQ is inserted in SCINT."),
        REGISTRIES,
    )
    assert decision is not None
    assert decision.category is EmailCategory.INTERNAL
    assert decision.rule == "R1_internal_only"


def test_r1_declines_when_one_recipient_is_external() -> None:
    """An employee mailing a customer is OUTBOUND_OWN, and the customer may reply
    into the same thread. The LLM makes that call, not a rule."""
    decision = match_fast_path(
        email(
            sender=EmailAddress(address="david.clark@our-company.com"),
            to=[
                EmailAddress(address="purchasing@new-company.com"),
                EmailAddress(address="michael.reed@our-company.com"),
            ],
        ),
        thread("Many thanks for your RFQ. We are pleased to submit our best offer."),
        REGISTRIES,
    )
    assert decision is None


def test_r2_empty_body_without_attachments() -> None:
    decision = match_fast_path(email(), thread(""), REGISTRIES)
    assert decision is not None
    assert decision.category is EmailCategory.OTHER_NON_ACTIONABLE


def test_r2_declines_when_an_attachment_carries_the_content() -> None:
    """PLAN.md trap 5: the RFQ lives in the XLSX and the body says nothing."""
    decision = match_fast_path(
        email(attachments=[Attachment(filename="E_QUOT_XLS_0015.XLSX")]),
        thread(""),
        REGISTRIES,
    )
    assert decision is None


def test_ordinary_customer_rfq_reaches_the_llm() -> None:
    assert match_fast_path(email(), thread(), REGISTRIES) is None


def test_no_sender_means_no_hard_rule_fires() -> None:
    """Without a header block a rule cannot be irrefutable, so it stays silent."""
    assert match_fast_path(email(sender=None), thread(), REGISTRIES) is None


def test_our_own_outgoing_quote_is_left_to_the_model() -> None:
    """A rule used to bin these. It also binned a supplier's live prices, so it went."""
    decision = match_fast_path(
        email(
            sender=EmailAddress(address="system@our-company.com"),
            to=[EmailAddress(address="sales@math-company.com")],
        ),
        thread("Quote received from SUPPLIER: OASIS CHEMICAL - Reference 9.41"),
        REGISTRIES,
    )
    assert decision is None


# --------------------------------------------------------------------------- #
# Hints
# --------------------------------------------------------------------------- #


def test_hints_describe_a_customer_rfq() -> None:
    hints = build_hints(
        email(attachments=[Attachment(filename="E_QUOT_XLS_0015.XLSX")]),
        thread('our RFQ for Department "Engine Materials", quotation due date is 02/June/2026'),
        REGISTRIES,
    )
    assert hints.sender_class is SenderClass.EXTERNAL_UNKNOWN
    assert hints.attachment_kinds == [AttachmentKind.RFQ_FORM_XLSX]
    assert hints.signals.department_or_category == "Engine Materials"
    assert hints.signals.quote_due_date == "02/June/2026"


def test_subject_prefixes_are_read_in_order() -> None:
    hints = build_hints(email(subject="RE: FW: [Updated]VSL: NORTH STAR"), thread(), REGISTRIES)
    assert hints.subject_prefixes == ["RE:", "FW:", "[UPDATED]"]


def test_recipients_internal_only_is_false_with_one_outsider() -> None:
    hints = build_hints(
        email(
            to=[EmailAddress(address="michael.reed@our-company.com")],
            cc=[EmailAddress(address="purchasing@new-company.com")],
        ),
        thread(),
        REGISTRIES,
    )
    assert hints.recipients_are_internal_only is False


def test_rendered_hints_are_readable_lines() -> None:
    rendered = render_hints(build_hints(email(), thread(), REGISTRIES))
    assert "sender_class: EXTERNAL_UNKNOWN" in rendered
    assert "thread: is_reply=false, quoted_messages=0" in rendered
