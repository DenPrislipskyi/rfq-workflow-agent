"""What the model is allowed to answer with when judging a row of the sheet.

Indexed rather than positional: a model that drops or merges an entry in a list
of fifty silently shifts every judgement after it, and a shifted judgement
confirms somebody else's product. The index makes that detectable instead.
"""

from pydantic import BaseModel, Field


class Judgement(BaseModel):
    """Whether one row's two descriptions are about one product."""

    # The number the prompt gave this pair. Not a row number of the RFQ and not
    # a row number of the sheet - the three are kept apart on purpose, so that
    # a renumbering anywhere cannot quietly re-point an answer.
    index: int
    same: bool
    # One sentence, and it goes into the record beside the outcome. This is what
    # an operator reads when they want to know why a quoted code was dropped.
    why: str = ""


class Judgements(BaseModel):
    items: list[Judgement] = Field(default_factory=list)
