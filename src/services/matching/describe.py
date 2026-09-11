"""A: the customer's line, said in our own words.

The catalogue is searched with words, and the two sides of that search are
written by different people: a customer types "Convex rulers", the shelf says
"RULE CONVEX STEEL METRIC 5MTR". This narrows that gap before the search rather
than asking the ranking to bridge it.

It never replaces the original. The verbatim line stays beside the restated one
all the way into the record - that is rule 7 of this project, and it is what
makes a wrong match explainable afterwards.

Whether it is worth its model call is a measured question, not an assumed one:
`describe_lines` writes the restatements and `catalog_recall` scores them
against the same cases as the raw wording.
"""

import logging
from collections.abc import Sequence

from src.infrastructure.llm.client import LLM
from src.infrastructure.llm.exceptions import LLMError
from src.services.matching.prompt import build_messages
from src.services.matching.schemas import RestatedLines

logger = logging.getLogger(__name__)

# Lines per call. One call for a whole RFQ would be cheaper still, but a model
# answering about two hundred lines at once starts dropping them, and a dropped
# line is worse than a second call: it falls back to its original wording and
# nobody is told why the match got worse.
BATCH = 50
# A restatement far longer than what it restates is the model explaining rather
# than answering. The original is the safer of the two.
MAX_GROWTH = 3


class LineDescriber:
    """Restates RFQ lines in the vocabulary the catalogue uses."""

    def __init__(self, llm: LLM, batch: int = BATCH) -> None:
        self._llm = llm
        self._batch = batch

    async def run(self, wordings: Sequence[str]) -> list[str]:
        """One restatement per line, in the order they came in.

        Never raises and never shortens the list. Anything the model does not
        answer for - a failed call, a dropped line, an index it invented - keeps
        the customer's own words, which is exactly the behaviour we would have
        had without this step at all.
        """
        restated = list(wordings)

        for start in range(0, len(wordings), self._batch):
            batch = list(wordings[start : start + self._batch])
            for index, text in (await self._restate(batch)).items():
                if 0 <= index < len(batch) and _worth_keeping(text, batch[index]):
                    restated[start + index] = text

        return restated

    async def _restate(self, batch: Sequence[str]) -> dict[int, str]:
        """One call. An empty answer means "keep what you had"."""
        try:
            answer = await self._llm.invoke(build_messages(batch), RestatedLines)
        except LLMError as error:
            logger.warning("Could not restate %d line(s): %s", len(batch), error)
            return {}

        return {line.index: line.description.strip() for line in answer.value.lines}


def _worth_keeping(restated: str, original: str) -> bool:
    """Whether the model's answer is better than the words it was given.

    Two ways it is not: nothing at all, and a paragraph. Both mean the model
    stopped restating, and the original has never been wrong about what the
    customer asked for.
    """
    return bool(restated) and len(restated) <= MAX_GROWTH * max(len(original), 20)
