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
