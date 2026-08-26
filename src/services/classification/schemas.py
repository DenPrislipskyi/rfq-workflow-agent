"""What the model is allowed to answer with.

`requires_action` and `recommended_action` are derived from the category by
`domain.policy`, so they are not the model's to decide; `is_rfq` is asked for
anyway as a cross-check. No `Field` constraints here: structured-output modes
drop keywords like `maxLength`, so the limits live in the prompt text and
`policy.finalize` clamps `confidence`.
"""

from pydantic import BaseModel, Field

from src.domain.enums import Direction, EmailCategory, Priority


class LLMClassification(BaseModel):
    """The model's verdict on the newest message of one email."""

    category: EmailCategory
    direction: Direction
    is_rfq: bool
    confidence: float
    priority_hint: Priority = Priority.NORMAL
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)
