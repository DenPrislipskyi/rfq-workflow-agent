"""Classify one email and record the decision.

Both entry points come through here, because the journal call is easy to forget
when adding one and a missing line fails silently. This is also where Phase 2
attaches: RFQ extraction will run after `classify`, and both entry points get it
at once.
"""

import logging
from dataclasses import dataclass

from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.infrastructure.storage.decisions import DecisionLog
from src.services.classification.pipeline import ClassificationPipeline

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TriagedEmail:
    """The verdict, plus the id of the journal line it was written to."""

    outcome: ClassificationOutcome
    decision_id: str | None


class EmailTriage:
    """The one use case of this service."""

    def __init__(
        self,
        pipeline: ClassificationPipeline,
        decisions: DecisionLog,
        prompt_version: str,
    ) -> None:
        self._pipeline = pipeline
        self._decisions = decisions
        self._prompt_version = prompt_version

    async def run(self, email: NormalizedEmail, *, source: str) -> TriagedEmail:
        """Classify, journal, log. Raises `LLMError` if the model cannot answer."""
        outcome = await self._pipeline.classify(email)
        decision_id = await self._decisions.record(
            source=source,
            email=email,
            outcome=outcome,
            prompt_version=self._prompt_version,
        )

        logger.info(
            "Classified | %s | %s | %s | %.2f | %s | %s",
            source,
            email.subject or "(no subject)",
            outcome.result.category,
            outcome.result.confidence,
            outcome.result.decision_path,
            decision_id or "not journalled",
        )
        return TriagedEmail(outcome=outcome, decision_id=decision_id)
