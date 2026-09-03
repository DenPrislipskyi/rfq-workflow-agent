"""Append-only journal of what the agent did: one JSON object per line.

Two kinds of line, told apart by `type` and tied together by `decision_id`:
`decision` the moment a verdict exists, `delivery` afterwards, once it is known
where the email went - nothing is ever rewritten, so a failed delivery is a
second line rather than an edit of the first. `LOG_EMAIL_BODIES=false` hashes
the body but keeps `reasoning` and `evidence`, which quote the email
near-verbatim, so treat the file as confidential rather than anonymised.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from src.domain.enums import DeliveryOutcome
from src.domain.models import ClassificationOutcome, NormalizedEmail

logger = logging.getLogger(__name__)

DECISION = "decision"
DELIVERY = "delivery"


class LoggedEmail(BaseModel):
    """The email as it is kept on disk.

    The body becomes its SHA-256 unless `LOG_EMAIL_BODIES` is on - enough to
    spot duplicates and identify the exact text classified. Subject, sender and
    attachment names are kept regardless: they make a line findable.
    """

    message_id: str | None = None
    mailbox: str | None = None
    sender: str | None = None
    subject: str | None = None
    body_sha256: str
    body_chars: int
    attachment_names: list[str] = Field(default_factory=list)
    body_text: str | None = None


class DecisionLog:
    """Appends one line per event to a JSONL file."""

    def __init__(self, path: Path, *, enabled: bool, log_bodies: bool) -> None:
        self._path = path
        self._enabled = enabled
        self._log_bodies = log_bodies
        # One writer at a time: a line with an email body can exceed the 4 KB
        # O_APPEND writes atomically, and two half-written lines corrupt the
        # journal for good.
        self._lock = asyncio.Lock()

        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Decisions are journalled to %s", path)

    async def record(
        self,
        *,
        source: str,
        email: NormalizedEmail,
        outcome: ClassificationOutcome,
        prompt_version: str,
    ) -> str | None:
        """Append one decision and return its id, or None when journalling is off.

        The id ties a response the caller received to the line on disk, so a
        wrong answer can be looked up rather than described from memory.
        """
        if not self._enabled:
            return None

        decision_id = str(uuid4())
        await self._append(
            {
                "type": DECISION,
                "decision_id": decision_id,
                "recorded_at": _now(),
                "source": source,
                "email": self._redact(email).model_dump(),
                "result": outcome.result.model_dump(mode="json"),
                "thread": _thread(outcome),
                "hints": _hints(outcome),
                "meta": {
                    "prompt_version": prompt_version,
                    "model": outcome.model,
                    "latency_ms": outcome.latency_ms,
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                },
            }
        )
        return decision_id

    async def record_delivery(
        self,
        *,
        decision_id: str | None,
        outcome: DeliveryOutcome,
        forwarded_to: str | None = None,
        cc: Sequence[str] = (),
        region: str | None = None,
        region_rule: str | None = None,
        labels: Sequence[str] = (),
    ) -> None:
        """Append what became of an RFQ, against the decision that produced it.

        A second line rather than an edit of the first: the journal is
        append-only, and a decision must survive even when the delivery that
        follows it does not.
        """
        if not self._enabled or decision_id is None:
            return

        await self._append(
            {
                "type": DELIVERY,
                "decision_id": decision_id,
                "recorded_at": _now(),
                "outcome": outcome.value,
                "forwarded_to": forwarded_to,
                "cc": list(cc),
                "region": region,
                "region_rule": region_rule,
                "labels": list(labels),
            }
        )

    def records(self) -> Iterator[dict[str, Any]]:
        """Every line, parsed, of both kinds."""
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    yield json.loads(line)

    def decisions(self) -> Iterator[dict[str, Any]]:
        """Verdicts only. Lines written before `type` existed count as verdicts."""
        return (item for item in self.records() if item.get("type", DECISION) == DECISION)

    def deliveries(self) -> Iterator[dict[str, Any]]:
        """Where the RFQs went, one line each."""
        return (item for item in self.records() if item.get("type") == DELIVERY)

    def _redact(self, email: NormalizedEmail) -> LoggedEmail:
        return LoggedEmail(
            message_id=email.message_id,
            mailbox=email.mailbox,
            sender=email.sender.address if email.sender else None,
            subject=email.subject,
            body_sha256=_digest(email.body_text),
            body_chars=len(email.body_text),
            attachment_names=[item.filename for item in email.attachments],
            body_text=email.body_text if self._log_bodies else None,
        )

    async def _append(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as file:
                file.write(line)


def _thread(outcome: ClassificationOutcome) -> dict[str, Any]:
    """How the email was cut up.

    Thread splitting is the likeliest thing to have gone wrong behind a bad
    verdict, and without these three numbers a reader cannot tell the model's
    mistake from the splitter's.
    """
    return {
        "is_reply": outcome.thread.is_reply,
        "quoted_messages": len(outcome.thread.quoted_messages),
        "latest_chars": len(outcome.thread.latest_message),
    }


def _hints(outcome: ClassificationOutcome) -> dict[str, Any]:
    """The deterministic signals the prompt was given.

    Only the ones that vary and steer an answer. Regex findings are left out -
    they are already stored under `result.extracted`.
    """
    hints = outcome.hints
    return {
        "sender_class": hints.sender_class.value,
        "portal": hints.portal,
        "subject_prefixes": hints.subject_prefixes,
        "attachment_kinds": [kind.value for kind in hints.attachment_kinds],
    }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
