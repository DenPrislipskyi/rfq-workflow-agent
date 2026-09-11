"""What the model is allowed to answer with when restating a line.

Indexed rather than positional: a model that drops or merges an item in a list
of fifty silently shifts every line after it, and a shifted line is matched to
somebody else's product. The index makes that detectable instead.
"""

from pydantic import BaseModel, Field


class RestatedLine(BaseModel):
    """One line of an RFQ, in the words our own catalogue would use."""

    # The number the prompt gave this line. Not the row number of the RFQ -
    # the two are kept apart on purpose, so a renumbering upstream cannot
    # quietly re-point an answer.
    index: int
    description: str


class RestatedLines(BaseModel):
    lines: list[RestatedLine] = Field(default_factory=list)


class ScoredCandidate(BaseModel):
    """One of the products a line was shown, and how sure the model is of it."""

    item_code: str
    # 0-100. The model's own opinion of itself, and worth reading as exactly
    # that: it is not a probability, it is not calibrated against anything, and
    # nothing decides on it. An operator sees it; no threshold acts on it.
    confidence: int = 0


class ChosenItem(BaseModel):
    """One line of an RFQ, matched to one product - or to none.

    `item_code` is nullable because "none of these" is a real answer and has to
    be as easy for the model to give as any code. A schema that demanded a code
    would be asking it to guess.
    """

    index: int
    item_code: str | None = None
    # One sentence. It is what an operator reads when the match looks wrong,
    # and what makes a refusal reviewable instead of merely blank.
    why: str = ""
    # Every candidate scored, not only the chosen one: the operator reviewing a
    # refusal needs to see what was rejected and by how much.
    candidates: list[ScoredCandidate] = Field(default_factory=list)


class ChosenItems(BaseModel):
    lines: list[ChosenItem] = Field(default_factory=list)
