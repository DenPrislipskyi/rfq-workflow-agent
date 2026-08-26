"""The classification pipeline: L1 preprocessing, L2 rules, L3 model, L4 policy.

One instance serves both entry points, because both hand it the same
`NormalizedEmail`. It holds no state between calls: every email is classified
on its own, with a prompt built from scratch.
"""

import logging

from src.core.config import Settings
from src.domain.enums import DecisionPath
from src.domain.models import (
    ClassificationOutcome,
    ClassificationResult,
    Hints,
    NormalizedEmail,
    SplitThread,
)
from src.domain.policy import Draft, Thresholds, finalize
from src.domain.preprocessing.boilerplate import strip_boilerplate
from src.domain.preprocessing.thread import split_thread
from src.domain.rules.fast_path import FastPathDecision, match_fast_path
from src.domain.rules.hints import build_hints
from src.domain.rules.registries import Registries
from src.infrastructure.llm.client import LLM
from src.services.classification.prompt import build_messages
from src.services.classification.schemas import LLMClassification

logger = logging.getLogger(__name__)


class ClassificationPipeline:
    """Turns one normalized email into one decision."""

    def __init__(self, llm: LLM, registries: Registries, settings: Settings) -> None:
        self._llm = llm
        self._registries = registries
        self._settings = settings
        self._thresholds = Thresholds(
            auto=settings.CONFIDENCE_AUTO_THRESHOLD,
            review=settings.CONFIDENCE_REVIEW_THRESHOLD,
        )

    async def classify(self, email: NormalizedEmail) -> ClassificationOutcome:
        """Split, clean, try the hard rules, else ask the model, then apply the policy."""
        thread = split_thread(
            email.body_text,
            min_latest_chars=self._settings.MIN_LATEST_CHARS_FOR_VALID_SPLIT,
        )
        thread = _without_boilerplate(thread)
        hints = build_hints(email, thread, self._registries)

        if self._settings.FAST_PATH_ENABLED:
            decision = match_fast_path(email, thread, self._registries)
            if decision is not None:
                logger.debug("Fast path %s decided %s", decision.rule, decision.category)
                return ClassificationOutcome(
                    result=self._finalize(_from_rule(decision), email, thread, hints),
                    thread=thread,
                    hints=hints,
                )

        answer = await self._llm.invoke(
            build_messages(email, thread, hints, self._settings), LLMClassification
        )
        result = self._finalize(_from_model(answer.value), email, thread, hints)

        if "guardrail:is_rfq_mismatch" in result.rule_hits:
            logger.warning(
                "Model answered is_rfq=%s for category %s; the category map decided",
                answer.value.is_rfq,
                result.category,
            )

        return ClassificationOutcome(
            result=result,
            thread=thread,
            hints=hints,
            model=answer.model,
            latency_ms=answer.latency_ms,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
        )

    def _finalize(
        self, draft: Draft, email: NormalizedEmail, thread: SplitThread, hints: Hints
    ) -> ClassificationResult:
        """Hand the verdict to the deterministic policy, with the newest message's signals."""
        return finalize(
            draft,
            signals=hints.signals,
            attachment_kinds=hints.attachment_kinds,
            thresholds=self._thresholds,
            parse_warnings=[*email.parse_warnings, *thread.warnings],
        )


def _without_boilerplate(thread: SplitThread) -> SplitThread:
    """Drop banners, disclaimers and signatures from the newest message.

    Measured on the corpus: 74% less text on the RFQ emails and no regex signal
    lost, because the stripper will not cut a region holding a price, a quantity
    or a vessel name. Quoted history is left alone - it is context, not the
    thing being classified.
    """
    latest, applied = strip_boilerplate(thread.latest_message)
    if applied:
        logger.debug("Stripped from the newest message: %s", ", ".join(applied))
    return thread.model_copy(update={"latest_message": latest})


def _from_rule(decision: FastPathDecision) -> Draft:
    """A hard rule answers the category but never the RFQ question."""
    return Draft(
        category=decision.category,
        direction=decision.direction,
        confidence=decision.confidence,
        decision_path=DecisionPath.RULES_FAST_PATH,
        reasoning=f"Matched the deterministic rule {decision.rule}.",
        rule_hits=[decision.rule],
    )


def _from_model(answer: LLMClassification) -> Draft:
    return Draft(
        category=answer.category,
        direction=answer.direction,
        confidence=answer.confidence,
        decision_path=DecisionPath.LLM,
        is_rfq=answer.is_rfq,
        priority=answer.priority_hint,
        reasoning=answer.reasoning,
        evidence=answer.evidence,
    )
