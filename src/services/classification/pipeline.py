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
from src.services.extraction.models import ReadDocument

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

    def needs_the_model(self, email: NormalizedEmail) -> bool:
        """Whether the hard rules leave this email for the model to answer.

        Asked before anything is downloaded. Reading the attachments can only
        change an answer the model gives, so an email the rules already settled
        must not cost a single request - which is what keeps the fast path the
        cost guard it was built to be.

        The rules do run twice on such an email, once here and once inside
        `classify`. They are a thread split and a handful of regexes against a
        model call per attached file, and they are pure, so the second run
        cannot disagree with the first.
        """
        if not self._settings.FAST_PATH_ENABLED:
            return True
        return match_fast_path(email, self._read_thread(email), self._registries) is None

    async def classify(
        self,
        email: NormalizedEmail,
        *,
        llm: LLM | None = None,
        files: list[ReadDocument] | None = None,
    ) -> ClassificationOutcome:
        """Split, clean, try the hard rules, else ask the model, then apply the policy.

        `llm` replaces the configured one for this call only, so an eval can run
        the same corpus through two models. The pipeline still depends on the
        `LLM` protocol alone and knows nothing about the registry that chose it.

        `files` are the attachments after stage B has read them. They reach the
        model and nothing else: what a file holds is evidence, while the rules
        and the policy work on the message. An email whose body is "please find
        attached" carries no evidence at all, and this is where that evidence
        comes from.
        """
        thread = self._read_thread(email)
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

        answer = await (llm or self._llm).invoke(
            build_messages(email, thread, hints, self._settings, files=files),
            LLMClassification,
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

    def _read_thread(self, email: NormalizedEmail) -> SplitThread:
        """The body cut into messages and cleaned - what both the rules and the
        prompt work on, and the one place that decides what "the newest
        message" means."""
        thread = split_thread(
            email.body_text,
            min_latest_chars=self._settings.MIN_LATEST_CHARS_FOR_VALID_SPLIT,
        )
        return _without_boilerplate(thread)

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
