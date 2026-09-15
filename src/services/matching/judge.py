"""Whether a row of the sheet maps what it says it maps.

The sheet is a history of mappings, and some of them are wrong. Row 1 of
today's snapshot says a customer asked for `EXTERNAL HDD 4TB` and was sold
`ROD FISHING WITH FURTHER, DETAILS`; its own note calls it a negative mapping
scenario. A customer code that lands on a row like that must not carry its item
code into an order, and no amount of word counting tells that row apart from
`KRAFT CHEESE SLICES` against `CHEESE, SLICED 200 GRM KRAFT`, which is one
product written twice. Measured on the snapshot, both sit at 66.7% word
overlap. So the question goes to a model, and there is no threshold beside it:
two mechanisms answering one question is how nobody can explain the answer
afterwards.

What the model is given is two sentences. It never sees an item code, the
catalogue or the RFQ, so the worst it can do is say yes where a person would
say no - it cannot invent a product, because it is not asked to name one.
"""

import asyncio
import logging
from collections.abc import Sequence

from src.infrastructure.llm.client import LLM
from src.infrastructure.llm.exceptions import LLMError
from src.services.matching.prompt import build_messages
from src.services.matching.schemas import Judgements

logger = logging.getLogger(__name__)

# Pairs per call. Not a context limit - fifty pairs of these descriptions is
# about nine hundred tokens, and the window would take ten times that. It is a
# limit on how long a numbered list a model answers without quietly dropping an
# entry, and on how much one failed call costs. `describe.py` batched at fifty
# for the same reason; two different numbers for one question would be worse
# than one.
BATCH = 50
# Batches in flight at once. Two hundred lines then cost the latency of one
# call rather than four, and nothing downstream is waiting on any of them.
CONCURRENCY = 4
# What stands in for a verdict nobody gave. It travels into the record beside
# the branch's own reason, so that "we did not check" reads differently from
# "we checked and it disagreed".
UNANSWERED = "The sheet row could not be checked, so its code was not trusted."


class AgreementJudge:
    """Says, for each row handed to it, whether its two descriptions agree."""

    def __init__(
        self, llm: LLM, *, batch: int = BATCH, concurrency: int = CONCURRENCY
    ) -> None:
        self._llm = llm
        self._batch = batch
        self._concurrency = concurrency

    async def run(self, pairs: Sequence[tuple[str, str]]) -> list[tuple[bool, str]]:
        """One verdict per pair, in the order they came in.

        Never raises and never shortens the list. Anything the model does not
        answer for - a failed call, a dropped index, an index it invented - is
        **not agreed**, which sends that line to the shortlist and shows an
        operator five candidates. The other default would confirm a code
        nobody checked, and silence is not consent.
        """
        verdicts: list[tuple[bool, str]] = [(False, UNANSWERED)] * len(pairs)
        if not pairs:
            return verdicts

        starts = range(0, len(pairs), self._batch)
        limit = asyncio.Semaphore(self._concurrency)
        answered = await asyncio.gather(
            *(self._one(pairs[start : start + self._batch], limit) for start in starts)
        )

        for start, judged in zip(starts, answered, strict=True):
            for index, verdict in judged.items():
                if 0 <= index < min(self._batch, len(pairs) - start):
                    verdicts[start + index] = verdict

        return verdicts

    async def _one(
        self, batch: Sequence[tuple[str, str]], limit: asyncio.Semaphore
    ) -> dict[int, tuple[bool, str]]:
        """One call. An empty answer means every pair in it stays unagreed."""
        async with limit:
            try:
                answer = await self._llm.invoke(build_messages(batch), Judgements)
            except LLMError as error:
                logger.warning("Could not judge %d row(s): %s", len(batch), error)
                return {}

        return {one.index: (one.same, one.why.strip()) for one in answer.value.items}

