"""Append-only journal of every decision: one JSON object per line, never rewritten.

A debugging trail today and the dataset for the next prompt later, which is why
it exists before it is needed; `record()` is the only entry point, so a database
can replace the file without touching anything above. `LOG_EMAIL_BODIES=false`
hashes the body but keeps `reasoning` and `evidence`, which quote the email
near-verbatim - treat the file as confidential, not anonymised.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from src.domain.models import ClassificationOutcome, NormalizedEmail

logger = logging.getLogger(__name__)


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
    """Writes one line per decision to a JSONL file."""

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
                "decision_id": decision_id,
                "recorded_at": _now(),
                "source": source,
                "email": self._redact(email).model_dump(),
                "result": outcome.result.model_dump(mode="json"),
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

    def records(self) -> Iterator[dict[str, Any]]:
        """Every line, parsed. How anything reads the journal back."""
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    yield json.loads(line)

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


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
