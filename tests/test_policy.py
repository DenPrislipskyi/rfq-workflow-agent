"""L4 policy: derivation, confidence bands and the RFQ safety net.

Pure functions, no model involved - every branch here has to be provable without
a network call, because these are the rules that stop a wrong verdict from
becoming a wrong action.
"""

import pytest

from src.domain.enums import (
    AttachmentKind,
    DecisionPath,
    Direction,
    EmailCategory,
    Priority,
    RecommendedAction,
)
from src.domain.models import Signals
from src.domain.policy import CATEGORY_ACTION, RFQ_CATEGORIES, Draft, Thresholds, finalize

THRESHOLDS = Thresholds(auto=0.85, review=0.60)


def draft(**overrides) -> Draft:
    """A confident customer clarification; override only what the test is about."""
    defaults = {
        "category": EmailCategory.CUSTOMER_CLARIFICATION,
        "direction": Direction.INBOUND_CUSTOMER,
        "confidence": 0.95,
        "decision_path": DecisionPath.LLM,
    }
    return Draft(**(defaults | overrides))


def run(draft_: Draft, *, signals: Signals | None = None, attachments=()) -> object:
    return finalize(
        draft_,
        signals=signals or Signals(),
        attachment_kinds=list(attachments),
        thresholds=THRESHOLDS,
    )


# --------------------------------------------------------------------------- #
# The category map
# --------------------------------------------------------------------------- #


def test_every_category_has_an_action() -> None:
    """A category with no entry would raise KeyError on a live email."""
    assert set(CATEGORY_ACTION) == set(EmailCategory)


@pytest.mark.parametrize("category", sorted(RFQ_CATEGORIES))
def test_rfq_categories_go_to_dst(category: EmailCategory) -> None:
    assert CATEGORY_ACTION[category] is RecommendedAction.FORWARD_TO_DST


@pytest.mark.parametrize("category", list(EmailCategory))
def test_is_rfq_marks_exactly_the_three_rfq_categories(category: EmailCategory) -> None:
    """`is_rfq` is a rule, not a column, so it cannot drift out of step with routing."""
    result = run(draft(category=category, confidence=0.95))
    assert result.is_rfq is (category in RFQ_CATEGORIES)


@pytest.mark.parametrize("category", list(EmailCategory))
def test_only_ignored_email_needs_no_action(category: EmailCategory) -> None:
    result = run(draft(category=category, confidence=0.95))
    assert result.requires_action is (result.recommended_action is not RecommendedAction.IGNORE)


def test_routing_fields_come_from_the_category_not_the_draft() -> None:
    result = run(draft(category=EmailCategory.NEW_RFQ))
    assert result.requires_action is True
    assert result.is_rfq is True
    assert result.recommended_action is RecommendedAction.FORWARD_TO_DST


def test_an_email_the_guardrails_hand_to_a_person_does_require_action() -> None:
    """An internal note the model is unsure about still has to be routed somewhere.

    Saying "no action needed" while sending it to HUMAN_REVIEW is a contradiction,
    which is why `requires_action` follows the final action rather than the category.
    """
    result = run(draft(category=EmailCategory.INTERNAL, confidence=0.40))

    assert result.recommended_action is RecommendedAction.HUMAN_REVIEW
    assert result.requires_action is True


def test_a_model_contradicting_the_map_is_recorded_and_overruled() -> None:
    result = run(draft(category=EmailCategory.NEW_RFQ, is_rfq=False))
    assert result.is_rfq is True
    assert "guardrail:is_rfq_mismatch" in result.rule_hits


def test_no_mismatch_hit_when_the_fast_path_stays_silent() -> None:
    """The rules never answer the RFQ question, so they can never contradict it."""
    result = run(draft(decision_path=DecisionPath.RULES_FAST_PATH, is_rfq=None))
    assert "guardrail:is_rfq_mismatch" not in result.rule_hits


# --------------------------------------------------------------------------- #
# Confidence bands
# --------------------------------------------------------------------------- #


def test_high_confidence_decides_on_its_own() -> None:
    result = run(draft(confidence=0.95))
    assert result.needs_human_review is False
    assert result.recommended_action is RecommendedAction.ROUTE_TO_CS


def test_middle_confidence_keeps_the_route_but_asks_for_a_look() -> None:
    result = run(draft(confidence=0.70))
    assert result.needs_human_review is True
    assert result.recommended_action is RecommendedAction.ROUTE_TO_CS
    assert "guardrail:confidence_below_auto" in result.rule_hits


def test_low_confidence_routes_to_a_human_instead() -> None:
    result = run(draft(confidence=0.40))
    assert result.needs_human_review is True
    assert result.recommended_action is RecommendedAction.HUMAN_REVIEW
    assert "guardrail:low_confidence" in result.rule_hits


def test_confidence_out_of_range_is_clamped() -> None:
    """The schema sent to the model carries no bounds, so a 1.4 is possible."""
    assert run(draft(confidence=1.4)).confidence == 1.0
    assert run(draft(confidence=-0.2)).confidence == 0.0


# --------------------------------------------------------------------------- #
# The RFQ safety net
# --------------------------------------------------------------------------- #


IGNORED = {"category": EmailCategory.OUTBOUND_OWN}

STRONG_SIGNALS = [
    ("rfq form attachment", {"attachments": [AttachmentKind.RFQ_FORM_XLSX]}),
    ("quote due date", {"signals": Signals(quote_due_date="02/June/2026")}),
    ("rfq reference", {"signals": Signals(rfq_reference="BH/O-0001/RFQ26")}),
]


@pytest.mark.parametrize(("name", "kwargs"), STRONG_SIGNALS, ids=[n for n, _ in STRONG_SIGNALS])
def test_an_rfq_signal_in_an_ignored_email_forces_a_second_look(name: str, kwargs) -> None:
    """Missing a real RFQ costs a sale; a false alarm costs five seconds."""
    result = run(draft(**IGNORED), **kwargs)
    assert result.needs_human_review is True
    assert result.decision_path is DecisionPath.LLM_WITH_GUARDRAIL_OVERRIDE
    assert "guardrail:rfq_signal_in_ignored_email" in result.rule_hits


def test_the_safety_net_leaves_actionable_emails_alone() -> None:
    result = run(draft(category=EmailCategory.NEW_RFQ), signals=Signals(rfq_reference="X-1"))
    assert result.needs_human_review is False
    assert result.decision_path is DecisionPath.LLM


def test_an_attachment_that_is_not_an_rfq_form_is_not_a_strong_signal() -> None:
    result = run(draft(**IGNORED), attachments=[AttachmentKind.OTHER, AttachmentKind.SIGNATURE_IMAGE])
    assert result.needs_human_review is False


def test_the_override_does_not_relabel_a_path_the_model_never_walked() -> None:
    """Saying "LLM" about a rule-only decision would make the decision log lie."""
    result = run(
        draft(**IGNORED, decision_path=DecisionPath.RULES_FAST_PATH),
        attachments=[AttachmentKind.RFQ_FORM_PDF],
    )
    assert result.needs_human_review is True
    assert result.decision_path is DecisionPath.RULES_FAST_PATH


def test_low_confidence_alone_does_not_trip_the_safety_net() -> None:
    """HUMAN_REVIEW is not IGNORE - the email is already going to a person."""
    result = run(draft(**IGNORED, confidence=0.3), signals=Signals(rfq_reference="X-1"))
    assert "guardrail:rfq_signal_in_ignored_email" not in result.rule_hits
    assert result.recommended_action is RecommendedAction.HUMAN_REVIEW


# --------------------------------------------------------------------------- #
# Priority and pass-through
# --------------------------------------------------------------------------- #


def test_urgency_markers_outrank_the_models_hint() -> None:
    result = run(draft(priority=Priority.LOW), signals=Signals(urgency_markers=["U R G E N T"]))
    assert result.priority is Priority.URGENT


def test_the_models_hint_survives_when_no_marker_was_found() -> None:
    assert run(draft(priority=Priority.LOW)).priority is Priority.LOW


def test_reasoning_evidence_and_signals_reach_the_result() -> None:
    signals = Signals(vessel_name="NORTH STAR")
    result = run(draft(reasoning="because", evidence=["a", "b"]), signals=signals)
    assert result.reasoning == "because"
    assert result.evidence == ["a", "b"]
    assert result.extracted.vessel_name == "NORTH STAR"


def test_finalize_does_not_mutate_the_draft() -> None:
    original = draft(confidence=0.5, rule_hits=["R9_something"])
    run(original)
    assert original.rule_hits == ["R9_something"]
