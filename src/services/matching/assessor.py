"""What a model observes about each candidate on a line's shortlist.

The same shape as `AgreementJudge`, for the same reasons: lines go in batches,
a few batches in flight at once, and nothing here raises. What differs is the
question - not "is this the product" but "what does each side say about each
property" - and that the answer is facts for `confidence.py` to score, never a
score.

A line comes back assessed only if every one of its candidates was answered
for. Half a shortlist cannot be scored: whether a candidate is "one of four"
depends on the other three.
"""

import asyncio
import logging
from collections.abc import Sequence

from src.infrastructure.llm.client import LLM
from src.infrastructure.llm.exceptions import LLMError
from src.services.matching.assess_prompt import build_messages
from src.services.matching.confidence import Agreement, Assessment, Attribute
from src.services.matching.schemas import CandidateCheck, LineCheck, LineChecks, PropertyCheck

logger = logging.getLogger(__name__)

# Lines per call. Each carries up to five candidates, so twenty lines ask about
# as many products as the judge's fifty pairs do.
BATCH = 20
CONCURRENCY = 4

# One line and the descriptions of its candidates, in shortlist order.
type Shortlist = tuple[str, Sequence[str]]


class CandidateAssessor:
    """Observes, for each line, every candidate on its shortlist."""

    def __init__(self, llm: LLM, *, batch: int = BATCH, concurrency: int = CONCURRENCY) -> None:
        self._llm = llm
        self._batch = batch
        self._concurrency = concurrency

    async def run(self, lines: Sequence[Shortlist]) -> list[list[Assessment] | None]:
        """One entry per line, in order: its candidates' assessments, or None
        where the model did not account for all of them."""
        assessed: list[list[Assessment] | None] = [None] * len(lines)
        # A line with nothing to assess needs no call, and has nothing missing.
        asked = [index for index, (_, candidates) in enumerate(lines) if candidates]
        for index, (_, candidates) in enumerate(lines):
            if not candidates:
                assessed[index] = []
        if not asked:
            return assessed

        batches = [
            asked[start : start + self._batch] for start in range(0, len(asked), self._batch)
        ]
        limit = asyncio.Semaphore(self._concurrency)
        answers = await asyncio.gather(
            *(self._one([lines[index] for index in batch], limit) for batch in batches)
        )
        for batch, answer in zip(batches, answers, strict=True):
            for position, index in enumerate(batch):
                assessed[index] = answer.get(position)
        return assessed

    async def _one(
        self, batch: Sequence[Shortlist], limit: asyncio.Semaphore
    ) -> dict[int, list[Assessment]]:
        """One call. A failed call leaves every line in it unassessed."""
        async with limit:
            try:
                answer = await self._llm.invoke(build_messages(batch), LineChecks)
            except LLMError as error:
                logger.warning("Could not assess %d line(s): %s", len(batch), error)
                return {}

        found: dict[int, list[Assessment]] = {}
        for line in answer.value.items:
            if 0 <= line.index < len(batch) and line.index not in found:
                complete = _complete(line, len(batch[line.index][1]))
                if complete is not None:
                    found[line.index] = complete
        return found


def _complete(line: LineCheck, count: int) -> list[Assessment] | None:
    """Every candidate's assessment in shortlist order, or None if any is
    missing. An answer for a candidate that was never offered is ignored: the
    model is not allowed to add products, only to describe them."""
    by_number: dict[int, CandidateCheck] = {}
    for one in line.candidates:
        if 1 <= one.candidate <= count:
            by_number.setdefault(one.candidate, one)
    if len(by_number) != count:
        return None
    return [_assessment(by_number[number]) for number in range(1, count + 1)]


def _assessment(check: CandidateCheck) -> Assessment:
    return Assessment(
        same_product=check.same_product,
        attributes=tuple(
            attribute for one in check.properties if (attribute := _attribute(one)) is not None
        ),
        why=check.why.strip(),
    )


def _attribute(check: PropertyCheck) -> Attribute | None:
    """The agreement follows from which sides state a value - not from what the
    model calls it. A property neither side states tells nothing and is left out."""
    line, item = check.line_value.strip(), check.item_value.strip()
    if line and item:
        agreement = Agreement.MATCH if check.agrees else Agreement.CONFLICT
    elif item:
        agreement = Agreement.UNSTATED
    elif line:
        agreement = Agreement.UNCONFIRMED
    else:
        return None
    return Attribute(name=check.name, key=check.key, agreement=agreement, offered=item)
