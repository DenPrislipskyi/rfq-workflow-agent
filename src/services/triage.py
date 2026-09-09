"""Classify one email and record the decision.

Both entry points come through here, because the journal call is easy to forget
when adding one and a missing line fails silently. This is also where Phase 2
attaches: RFQ extraction will run after `classify`, and both entry points get it
at once.
"""

import logging
from dataclasses import dataclass

from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.infrastructure.llm.client import LLM
from src.infrastructure.storage.decisions import DecisionLog
from src.services.classification.pipeline import ClassificationPipeline
from src.services.extraction.models import ReadDocument

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

    def needs_the_model(self, email: NormalizedEmail) -> bool:
        """Whether the verdict on this email will cost a model call.

        Asked by the mailbox handler before it downloads anything: reading the
        attachments is only worth its cost when the model is the one deciding.
        """
        return self._pipeline.needs_the_model(email)

    async def run(
        self,
        email: NormalizedEmail,
        *,
        source: str,
        llm: LLM | None = None,
        files: list[ReadDocument] | None = None,
    ) -> TriagedEmail:
        """Classify, journal, log. Raises `LLMError` if the model cannot answer.

        `llm` overrides the configured model for this one email. The journal
        records which model answered either way, so a comparison run is
        readable afterwards without any extra bookkeeping.

        `files` are the attachments, already read, when the caller read them
        first. The verdict sees what is in them, and so does the journal line -
        without it, "why did this look like an RFQ?" is unanswerable for the
        emails whose whole demand was in a file.
        """
        outcome = await self._pipeline.classify(email, llm=llm, files=files)
        decision_id = await self._decisions.record(
            source=source,
            email=email,
            outcome=outcome,
            prompt_version=self._prompt_version,
            attachments=_seen(files),
        )

        logger.info(
            "Classified | %s | %s | %s | %.2f | %s | %s files | %s",
            source,
            email.subject or "(no subject)",
            outcome.result.category,
            outcome.result.confidence,
            outcome.result.decision_path,
            len(files) if files is not None else "no",
            decision_id or "not journalled",
        )
        return TriagedEmail(outcome=outcome, decision_id=decision_id)


def _seen(files: list[ReadDocument] | None) -> list[dict[str, object]] | None:
    """What the verdict was shown of the attachments, or None when it saw none.

    Short on purpose. The extraction line carries the full reading of every
    file, and the only question this one answers is whether the classifier knew
    what was in them - so it keeps what the prompt actually said.
    """
    if files is None:
        return None
    return [
        {
            "origin": found.origin,
            "role": found.role.value,
            "what": found.what,
            "items": len(found.items),
        }
        for found in files
    ]
