"""Put a sample RFQ in the database, so the screens have something to show.

    uv run python -m src.tools.make_sample_rfq

Everything the matching screen reads is produced by an email arriving, and an
email arriving needs a mailbox, a model and somebody to send one. This writes
the same record from the catalogue instead: real products, real descriptions,
and lines arranged to cover the three cases the screen has to draw -

    matched by the code the customer quoted
    matched on the words alone
    not matched, with the candidates that were considered

No model and no mailbox. The record is marked `source: sample` and its subject
says so, because a page full of invented RFQs that looks like real traffic is
worse than an empty one.
"""

import asyncio
import logging
import sys
from datetime import UTC, datetime

from src.core.config import get_settings
from src.core.logging import configure_logging
from src.domain.enums import (
    DecisionPath,
    Direction,
    EmailCategory,
    RecommendedAction,
)
from src.domain.models import (
    ClassificationOutcome,
    ClassificationResult,
    EmailAddress,
    Hints,
    NormalizedEmail,
    Signals,
    SplitThread,
)
from src.domain.rules.catalog import Catalog, CatalogItem
from src.infrastructure.catalog import Snapshot
from src.infrastructure.storage.records import (
    EmailRecords,
    RecordedCandidate,
    RecordedExtraction,
    RecordedMatch,
)

logger = logging.getLogger(__name__)

SAMPLE = "sample"
SENDER = "purchasing@almi.example.com"
VESSEL = "MV ALMI GLOBE"
SUBJECT = "[SAMPLE] RFQ / MV ALMI GLOBE / Jebel Ali"
BODY = (
    "This RFQ was not sent by anybody. It was written by "
    "`src.tools.make_sample_rfq` out of the product sheet, so that the "
    "matching screen has lines to draw before real mail arrives."
)
# What a customer would have asked for, and how much of it.
QUANTITIES = [("500", "set"), ("12", "pcs"), ("30", "prs"), ("4", "pcs"), ("1", "pc")]
# A line nothing in the catalogue answers. Its candidates are whatever the
# search turns up, which is the point: the screen has to draw them too.
UNMATCHED = "Marine diesel turbocharger cartridge NR34/S"


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    catalog = Catalog.from_rows(
        Snapshot(settings.CATALOG_SNAPSHOT_PATH).rows(),
        code_column=settings.CATALOG_CODE_COLUMN,
        description_column=settings.CATALOG_DESCRIPTION_COLUMN,
        customer_code_column=settings.CATALOG_CUSTOMER_CODE_COLUMN,
        customer_description_column=settings.CATALOG_CUSTOMER_DESCRIPTION_COLUMN,
    )
    if not len(catalog):
        logger.error("No catalogue - run `python -m src.tools.sync_catalog` first")
        return 1

    records = EmailRecords(settings.DATABASE_PATH, enabled=True)
    matching = _lines(catalog, settings.CATALOG_SHORTLIST)
    if not matching:
        logger.error("The catalogue has no product with a customer wording to sample from")
        return 1

    record_id = await records.open(
        email=_email(), outcome=_outcome(), decision_id=None, source=SAMPLE
    )
    if record_id is None:
        logger.error("DATABASE_ENABLED is false - there is nowhere to write a sample")
        return 1

    await records.update(
        record_id,
        labels=["SSG RFQ"],
        labelled=False,
        extraction=RecordedExtraction(
            items=len(matching),
            complete=False,
            header={
                "vessel_name": {"raw": VESSEL, "value": VESSEL, "source": "sample"},
                "imo": {"raw": "9417751", "value": "9417751", "source": "sample"},
                "delivery_port": {"raw": "Jebel Ali", "value": "Jebel Ali", "source": "sample"},
            },
        ),
        matching=matching,
    )

    logger.info("Wrote sample RFQ %s with %d line(s):", record_id, len(matching))
    for line in matching:
        logger.info(
            "  %-44s -> %-12s %s",
            line.verbatim[:44],
            line.item_code or "not matched",
            f"{line.confidence}%" if line.confidence is not None else f"{len(line.candidates)} candidates",
        )
    return 0


def _lines(catalog: Catalog, shortlist: int) -> list[RecordedMatch]:
    """One line per case the screen has to draw."""
    known = _believable(catalog)
    lines = [
        _matched(index, item, quantity, uom)
        for index, (item, (quantity, uom)) in enumerate(
            zip(known[: len(QUANTITIES)], QUANTITIES, strict=False), start=1
        )
    ]
    if lines:
        lines.append(_refused(len(lines) + 1, catalog, shortlist))
    return lines


def _believable(catalog: Catalog) -> list[CatalogItem]:
    """Products whose recorded wording actually finds them.

    The sheet holds rows the desk put there as examples of mappings that went
    *wrong* - a customer asking for a hard drive against a fishing rod - and a
    sample that showed those as confident matches would teach the wrong thing.
    Rather than knowing which rows those are, this asks the catalogue: a
    product its own wording does not find is not one to demonstrate with.
    """
    believable = []
    for item in catalog.items:
        if not item.customer_description or not item.code:
            continue
        found = catalog.search(item.customer_description, limit=1)
        if found and found[0].item.code == item.code:
            believable.append(item)
    return believable


def _matched(index: int, item: CatalogItem, quantity: str, uom: str) -> RecordedMatch:
    """A line the catalogue answers.

    The customer's own wording is the one the sheet recorded for this product,
    which is as close to a real request as this tool can honestly get.
    """
    by_code = bool(item.customer_code)
    return RecordedMatch(
        index=index,
        verbatim=item.customer_description,
        description=item.description,
        customer_code=item.customer_code or None,
        quantity=quantity,
        uom=uom,
        item_code=item.code,
        item_description=item.description,
        item=dict(item.fields),
        confidence=96 if by_code else 88,
        how="code_confirmed" if by_code else "search",
        why="Sample line: the sheet records this product against this wording.",
        candidates=[
            RecordedCandidate(
                item_code=item.code,
                description=item.description,
                confidence=96 if by_code else 88,
                item=dict(item.fields),
            )
        ],
    )


def _refused(index: int, catalog: Catalog, shortlist: int) -> RecordedMatch:
    """A line nothing answers, with the products that were considered."""
    candidates = catalog.search(UNMATCHED, limit=shortlist)
    return RecordedMatch(
        index=index,
        verbatim=UNMATCHED,
        description=UNMATCHED.upper(),
        quantity="1",
        uom="pc",
        how="none",
        why="Sample line: none of the candidates is a turbocharger cartridge.",
        candidates=[
            RecordedCandidate(
                item_code=candidate.item.code,
                description=candidate.item.description,
                # Falling scores, so the screen has something to draw. A real
                # one is the search's own ranking; this tool ranks nothing.
                confidence=max(5, 40 - 8 * position),
                item=dict(candidate.item.fields),
            )
            for position, candidate in enumerate(candidates)
        ],
    )


def _email() -> NormalizedEmail:
    return NormalizedEmail(
        message_id=f"SAMPLE-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        received_at=datetime.now(UTC),
        sender=EmailAddress(name="Purchasing", address=SENDER),
        subject=SUBJECT,
        body_text=BODY,
    )


def _outcome() -> ClassificationOutcome:
    return ClassificationOutcome(
        result=ClassificationResult(
            category=EmailCategory.NEW_RFQ,
            direction=Direction.INBOUND_CUSTOMER,
            requires_action=True,
            is_rfq=True,
            recommended_action=RecommendedAction.FORWARD_TO_DST,
            confidence=1.0,
            needs_human_review=False,
            decision_path=DecisionPath.RULES_FAST_PATH,
            reasoning="Written by the sample tool. No model was asked anything.",
            extracted=Signals(vessel_name=VESSEL, delivery_port="Jebel Ali"),
        ),
        thread=SplitThread(latest_message=BODY),
        hints=Hints(),
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
