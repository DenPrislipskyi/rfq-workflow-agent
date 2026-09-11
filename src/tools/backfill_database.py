"""Build `Database/` records out of the journal the agent has already written.

    uv run python -m src.tools.backfill_database

Every email the agent has ever classified is in `data/decisions.jsonl`, and
until now that was the only account of it. This walks those lines and writes
the record each one would have produced, so the page has the whole history in
it rather than starting empty and filling up one email at a time.

What it cannot recover, it leaves out rather than inventing: the journal keeps
the body's hash instead of the body unless `LOG_EMAIL_BODIES` was on, and it
never held the attachments' bytes at all. A backfilled record therefore names
its files and does not have them - which is what a record written today says
too about an email nobody downloaded.

Runs on the journal alone: no model, no mailbox, no network. Safe to run twice
in the sense that nothing is deleted, but it does not deduplicate - a second
run against the same journal writes a second set of folders, so run it into an
empty `Database/`.
"""

import asyncio
import logging
import sys
from collections import defaultdict
from typing import Any

from src.core.config import get_settings
from src.core.logging import configure_logging
from src.domain.models import (
    Attachment,
    ClassificationOutcome,
    ClassificationResult,
    EmailAddress,
    Hints,
    NormalizedEmail,
    SplitThread,
)
from src.domain.rules.registries import Registries
from src.infrastructure.storage.decisions import DecisionLog
from src.infrastructure.storage.records import (
    BACKFILLED,
    EmailRecords,
    RecordedDelivery,
    RecordedExtraction,
)

logger = logging.getLogger(__name__)


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    journal = DecisionLog(settings.DECISIONS_LOG_PATH, enabled=True, log_bodies=True)
    records = EmailRecords(
        settings.DATABASE_PATH, enabled=True, keep_attachments=settings.DATABASE_KEEP_ATTACHMENTS
    )
    registries = Registries.load(settings.REGISTRIES_PATH, mailbox=settings.MAILBOX_ADDRESS)

    lines = list(journal.records())
    if not lines:
        logger.warning("Nothing in %s to backfill from", settings.DECISIONS_LOG_PATH)
        return 1

    # The journal is append-only: what became of an email is a later line
    # against the same decision, so the lines have to be folded before any of
    # them can be written as one record.
    extras: dict[str, dict[str, Any]] = defaultdict(dict)
    for line in lines:
        if line.get("type") in {"extraction", "delivery"}:
            extras[line["decision_id"]][line["type"]] = line

    written = 0
    for line in lines:
        if line.get("type", "decision") != "decision":
            continue
        if await _write(records, registries, line, extras[line["decision_id"]]):
            written += 1

    logger.info("Wrote %d record(s) into %s", written, settings.DATABASE_PATH)
    return 0


async def _write(
    records: EmailRecords,
    registries: Registries,
    line: dict[str, Any],
    extras: dict[str, Any],
) -> bool:
    """One journal decision, plus whatever followed it, as one record."""
    try:
        result = ClassificationResult.model_validate(line["result"])
    except Exception:
        logger.exception("Skipping %s: its result will not parse", line.get("decision_id"))
        return False

    record_id = await records.open(
        email=_email(line["email"], line.get("recorded_at")),
        outcome=ClassificationOutcome(
            result=result,
            thread=SplitThread(latest_message=""),
            hints=Hints(),
            model=(line.get("meta") or {}).get("model"),
        ),
        decision_id=line["decision_id"],
        source=line.get("source", "outlook"),
        files_note=BACKFILLED,
    )
    if record_id is None:
        return False

    delivery = extras.get("delivery")
    await records.update(
        record_id,
        # From the delivery line when there is one, because that is what the
        # mailbox was actually asked to stamp. Otherwise the labels this
        # verdict earns, which is what the handler would have applied.
        labels=(delivery or {}).get("labels") or registries.outlook_categories.for_result(result),
        labelled=(delivery or {}).get("labelled"),
        delivery=_delivery(delivery),
        extraction=_extraction(extras.get("extraction")),
    )
    return True


def _email(payload: dict[str, Any], recorded_at: str | None) -> NormalizedEmail:
    """The email as the journal kept it.

    `received_at` is the journal's own timestamp: it recorded when the agent
    answered, never when the customer wrote, and the record's id sorts by this.
    """
    address = payload.get("sender")
    return NormalizedEmail(
        message_id=payload.get("message_id"),
        mailbox=payload.get("mailbox"),
        received_at=recorded_at,
        sender=EmailAddress(address=address) if address else None,
        subject=payload.get("subject"),
        # None unless LOG_EMAIL_BODIES was on when the line was written. The
        # hash beside it is not a body and is not worth showing as one.
        body_text=payload.get("body_text") or "",
        attachments=[Attachment(filename=name) for name in payload.get("attachment_names", [])],
    )


def _delivery(line: dict[str, Any] | None) -> RecordedDelivery | None:
    if line is None:
        return None
    return RecordedDelivery(
        outcome=line["outcome"],
        forwarded_to=line.get("forwarded_to"),
        cc=line.get("cc") or [],
        region=line.get("region"),
        region_rule=line.get("region_rule"),
        attached=line.get("attached"),
    )


def _extraction(line: dict[str, Any] | None) -> RecordedExtraction | None:
    if line is None:
        return None
    items = line.get("items") or {}
    return RecordedExtraction(
        items=int(items.get("count", 0)),
        complete=bool(line.get("complete")),
        missing_required=line.get("missing_required") or [],
        warnings=line.get("warnings") or [],
        header=line.get("header") or {},
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
