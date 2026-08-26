"""L4: the deterministic policy applied after a verdict is reached.

Both decision paths hand a `Draft` to `finalize`, which derives the three fields
a model must never own: `recommended_action` from the CATEGORY_ACTION table,
`is_rfq` from the category, `requires_action` from the action. "SPAM_MARKETING,
but forward it to DST" is impossible by construction.
"""

from typing import NamedTuple

from pydantic import BaseModel, Field

from src.domain.enums import (
    AttachmentKind,
    DecisionPath,
    Direction,
    EmailCategory,
    Priority,
    RecommendedAction,
)
from src.domain.models import ClassificationResult, Signals

# The single source of truth for routing: change a line here and the whole
# service routes differently, prompt untouched.
CATEGORY_ACTION: dict[EmailCategory, RecommendedAction] = {
    EmailCategory.NEW_RFQ: RecommendedAction.FORWARD_TO_DST,
    EmailCategory.UPDATED_RFQ: RecommendedAction.FORWARD_TO_DST,
    EmailCategory.PORTAL_RFQ_NOTIFICATION: RecommendedAction.FORWARD_TO_DST,
    EmailCategory.CUSTOMER_ORDER_PO: RecommendedAction.ROUTE_TO_ORDER_TEAM,
    EmailCategory.CUSTOMER_CLARIFICATION: RecommendedAction.ROUTE_TO_CS,
    EmailCategory.SUPPLIER_CORRESPONDENCE: RecommendedAction.ROUTE_TO_SOURCING,
    EmailCategory.QUOTE_STATUS_NOTIFICATION: RecommendedAction.ROUTE_TO_CS,
    EmailCategory.INTERNAL: RecommendedAction.IGNORE,
    EmailCategory.OUTBOUND_OWN: RecommendedAction.IGNORE,
    EmailCategory.SPAM_MARKETING: RecommendedAction.IGNORE,
    EmailCategory.AUTO_REPLY_SYSTEM: RecommendedAction.IGNORE,
    EmailCategory.OTHER_NON_ACTIONABLE: RecommendedAction.IGNORE,
    EmailCategory.UNCERTAIN: RecommendedAction.HUMAN_REVIEW,
}

# "A customer wants a price from us" - the only categories that reach DST.
RFQ_CATEGORIES = frozenset(
    {EmailCategory.NEW_RFQ, EmailCategory.UPDATED_RFQ, EmailCategory.PORTAL_RFQ_NOTIFICATION}
)

# On its own a reason to look again at an email we were about to ignore.
RFQ_FORM_ATTACHMENTS = frozenset({AttachmentKind.RFQ_FORM_XLSX, AttachmentKind.RFQ_FORM_PDF})


class Thresholds(NamedTuple):
    """Confidence bands, from Settings.

    Self-reported confidence is not a probability, so these are starting
    heuristics to be recalibrated once real traffic is logged.
    """

    auto: float
    review: float


class Draft(BaseModel):
    """A verdict before the policy runs. Both decision paths produce one."""

    category: EmailCategory
    direction: Direction
    confidence: float
    decision_path: DecisionPath
    # The model's own answer, cross-checked against RFQ_CATEGORIES and never
    # trusted. None from the fast path, which does not answer this question.
    is_rfq: bool | None = None
    priority: Priority = Priority.NORMAL
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)
    rule_hits: list[str] = Field(default_factory=list)


def finalize(
    draft: Draft,
    *,
    signals: Signals,
    attachment_kinds: list[AttachmentKind],
    thresholds: Thresholds,
    parse_warnings: list[str] | None = None,
) -> ClassificationResult:
    """Route the email, apply the confidence bands, then the safety net.

    `signals` and `attachment_kinds` must describe the newest message only - a
    quote due date found in quoted history is not evidence about this email.
    """
    is_rfq = draft.category in RFQ_CATEGORIES
    action = CATEGORY_ACTION[draft.category]
    decision_path = draft.decision_path
    needs_review = False
    hits = list(draft.rule_hits)

    if draft.is_rfq is not None and draft.is_rfq != is_rfq:
        hits.append("guardrail:is_rfq_mismatch")

    if draft.confidence < thresholds.review:
        action = RecommendedAction.HUMAN_REVIEW
        needs_review = True
        hits.append("guardrail:low_confidence")
    elif draft.confidence < thresholds.auto:
        needs_review = True
        hits.append("guardrail:confidence_below_auto")

    if action is RecommendedAction.IGNORE and _has_strong_rfq_signal(signals, attachment_kinds):
        needs_review = True
        hits.append("guardrail:rfq_signal_in_ignored_email")
        # Only relabel a path the LLM actually walked, or the journal would lie.
        if decision_path is DecisionPath.LLM:
            decision_path = DecisionPath.LLM_WITH_GUARDRAIL_OVERRIDE

    return ClassificationResult(
        category=draft.category,
        direction=draft.direction,
        # From the final action, not the category: an email the guardrails sent
        # to a person still needs the Mailbox Team to do something.
        requires_action=action is not RecommendedAction.IGNORE,
        is_rfq=is_rfq,
        recommended_action=action,
        confidence=_clamp(draft.confidence),
        needs_human_review=needs_review,
        priority=_priority(draft.priority, signals),
        decision_path=decision_path,
        reasoning=draft.reasoning,
        evidence=list(draft.evidence),
        extracted=signals,
        rule_hits=hits,
        parse_warnings=list(parse_warnings or []),
    )


def _has_strong_rfq_signal(signals: Signals, attachment_kinds: list[AttachmentKind]) -> bool:
    """Missing a real RFQ costs the chandler a sale; a false alarm costs an operator five seconds."""
    return bool(
        RFQ_FORM_ATTACHMENTS.intersection(attachment_kinds)
        or signals.quote_due_date
        or signals.rfq_reference
    )


def _priority(hinted: Priority, signals: Signals) -> Priority:
    """Regex urgency markers outrank the model's hint; nothing here downgrades it.

    PLAN 11.4 also wants URGENT when a quote is due within two days, but
    `quote_due_date` is still free-form text, so that half waits for date parsing.
    """
    return Priority.URGENT if signals.urgency_markers else hinted


def _clamp(confidence: float) -> float:
    """The schema sent to the model carries no numeric bounds, so enforce them here."""
    return min(max(confidence, 0.0), 1.0)
