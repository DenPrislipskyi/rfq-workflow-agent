"""D: which of the candidates is the product, if any of them is.

The first step in this pipeline where the agent decides something rather than
measures something. Everything before it narrowed the catalogue down to a
handful of products without spending a model call; this is the one judgement
that cannot be made by counting words - whether two descriptions of a thing are
descriptions of the same thing.

Two guardrails, and they are the reason this is not just a call:

    a code that was not offered      is refused, not stored
    a line that was not answered     is a refusal, not the top candidate

The first is the important one. An item code is a key: the desk orders against
it, and a model that invents one invents an order. Nothing reaches a record
that was not on the list the model was shown.
"""

import logging
from collections.abc import Sequence

from src.infrastructure.llm.client import LLM
from src.infrastructure.llm.exceptions import LLMError
from src.services.matching.models import Choice, Question
from src.services.matching.prompt import build_choice_messages
from src.services.matching.schemas import ChosenItem, ChosenItems

logger = logging.getLogger(__name__)

# Lines per call. Smaller than the restating step's batch, because each line
# here carries its candidates with it - twenty lines is already a hundred
# descriptions in one prompt.
BATCH = 20

NOT_OFFERED = "The model answered with {code}, which was not among this line's candidates."
NOT_ANSWERED = "The model did not answer for this line."
NOT_ASKED = "The catalogue had nothing to offer for this line."
FAILED = "The model could not be asked."
# One sentence is what an operator reads. Past this the model is explaining.
MAX_WHY_CHARS = 300


class ItemChooser:
    """Picks one product per line out of the shortlist, or none."""

    def __init__(self, llm: LLM, batch: int = BATCH) -> None:
        self._llm = llm
        self._batch = batch

    async def run(self, questions: Sequence[Question]) -> dict[int, Choice]:
        """One choice per question, by the index the question carries.

        Never raises. Every line gets an answer, and a line the model did not
        answer for gets a refusal with the reason - which is the same thing a
        human reviewer would want to know.
        """
        choices: dict[int, Choice] = {
            question.index: Choice(None, NOT_ASKED if not question.candidates else NOT_ANSWERED)
            for question in questions
        }

        askable = [question for question in questions if question.candidates]
        for start in range(0, len(askable), self._batch):
            batch = askable[start : start + self._batch]
            choices.update(await self._ask(batch))

        return choices

    async def _ask(self, batch: Sequence[Question]) -> dict[int, Choice]:
        """One call, and everything that comes back checked before it is kept."""
        try:
            answer = await self._llm.invoke(build_choice_messages(batch), ChosenItems)
        except LLMError as error:
            logger.warning("Could not match %d line(s): %s", len(batch), error)
            return {question.index: Choice(None, FAILED) for question in batch}

        offered = {question.index: question for question in batch}
        checked: dict[int, Choice] = {}

        for line in answer.value.lines:
            question = offered.get(line.index)
            if question is None:
                # An index for a line that is not in this batch. Ignored rather
                # than applied to whatever line happens to have that number.
                logger.warning("The model answered for line %d, which it was not asked about", line.index)
                continue
            checked[line.index] = _checked(line, question)

        return checked


def _checked(line: ChosenItem, question: Question) -> Choice:
    """The model's answer, or a refusal saying why it was not taken.

    An item code the model was not shown is the failure worth naming: the desk
    orders against this number, so one that came from nowhere must never reach
    a record. It is not corrected to the nearest candidate either - a model
    that answered off the list was not choosing from it.
    """
    reason = line.why.strip()[:MAX_WHY_CHARS]
    offered = {candidate.code for candidate in question.candidates}
    scores = {
        scored.item_code: max(0, min(100, scored.confidence))
        for scored in line.candidates
        if scored.item_code in offered
    }

    if not line.item_code:
        return Choice(None, reason or NOT_ANSWERED, scores)

    if line.item_code not in offered:
        logger.warning("Line %d: %s", question.index, NOT_OFFERED.format(code=line.item_code))
        return Choice(None, NOT_OFFERED.format(code=line.item_code), scores)

    return Choice(line.item_code, reason, scores)
