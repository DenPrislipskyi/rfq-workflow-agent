"""L2: the cases that need no LLM at all.

A rule belongs here only if it is logically irrefutable; anything less is a hint
instead, because a wrong fast-path decision never reaches the model that could
have corrected it. Both rules need a sender, so on a dump with no header block
the fast path declines and the LLM decides - silence, not a guess.
"""

from pydantic import BaseModel

from src.domain.enums import Direction, EmailCategory
from src.domain.models import NormalizedEmail, SplitThread
from src.domain.rules.registries import Registries


class FastPathDecision(BaseModel):
    """A verdict reached without the LLM. `rule` names which one fired."""

    category: EmailCategory
    direction: Direction
    confidence: float
    rule: str


def match_fast_path(
    email: NormalizedEmail, thread: SplitThread, registries: Registries
) -> FastPathDecision | None:
    """First matching hard rule, or None when the LLM has to decide."""
    for rule in (_internal_only, _empty_body):
        decision = rule(email, thread, registries)
        if decision is not None:
            return decision
    return None


def _internal_only(
    email: NormalizedEmail, thread: SplitThread, registries: Registries
) -> FastPathDecision | None:
    """R1 - our own staff writing to our own staff, nobody external on the thread.

    A single external recipient disqualifies it: an employee mailing a customer
    is OUTBOUND_OWN, and the customer may reply into the same thread.
    """
    domain = email.sender.domain if email.sender else None
    if not registries.is_internal_domain(domain):
        return None

    recipients = [*email.to, *email.cc]
    if not recipients:
        return None
    if not all(registries.is_internal_domain(person.domain) for person in recipients):
        return None

    return FastPathDecision(
        category=EmailCategory.INTERNAL,
        direction=Direction.INTERNAL,
        confidence=registries.fast_path.confidence.internal_only,
        rule="R1_internal_only",
    )


def _empty_body(
    email: NormalizedEmail, thread: SplitThread, registries: Registries
) -> FastPathDecision | None:
    """R2 - nothing to read and nothing attached."""
    if email.attachments:
        return None
    if len(thread.latest_message.strip()) >= registries.fast_path.min_body_chars:
        return None

    return FastPathDecision(
        category=EmailCategory.OTHER_NON_ACTIONABLE,
        direction=Direction.UNKNOWN,
        confidence=registries.fast_path.confidence.empty_body,
        rule="R2_empty_body",
    )
