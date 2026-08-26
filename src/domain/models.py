from datetime import datetime

from pydantic import BaseModel, Field

from src.domain.enums import (
    AttachmentKind,
    DecisionPath,
    Direction,
    EmailCategory,
    Priority,
    RecommendedAction,
    SenderClass,
)


class EmailAddress(BaseModel):
    name: str | None = None
    address: str | None = None

    @property
    def domain(self) -> str | None:
        """Sender domain - the strongest signal for deciding direction."""
        if not self.address or "@" not in self.address:
            return None
        return self.address.rsplit("@", 1)[1].lower()


class Attachment(BaseModel):
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    kind: AttachmentKind = AttachmentKind.OTHER
    # Phase 2: filled in by the attachment parser. Always None in Phase 1.
    extracted_text: str | None = None


class NormalizedEmail(BaseModel):
    """The one internal shape of an email.

    Every entry point maps into this - the HTTP endpoint and the Outlook
    webhook - and the pipeline sees nothing else.
    """

    message_id: str | None = None
    received_at: datetime | None = None
    mailbox: str | None = None
    region_hint: str | None = None
    sender: EmailAddress | None = None
    to: list[EmailAddress] = Field(default_factory=list)
    cc: list[EmailAddress] = Field(default_factory=list)
    subject: str | None = None
    body_text: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)


class QuotedMessage(BaseModel):
    """One older message quoted inside the thread."""

    raw: str
    from_address: str | None = None
    sent_at: str | None = None
    subject: str | None = None


class SplitThread(BaseModel):
    """An email cut into the newest message and the history quoted below it.

    Classification looks only at `latest_message`; the history is context.
    """

    latest_message: str
    quoted_messages: list[QuotedMessage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_reply(self) -> bool:
        return bool(self.quoted_messages)


class Signals(BaseModel):
    """Entities pulled out by regex. Hints for the prompt, never a verdict."""

    vessel_name: str | None = None
    imo: str | None = None
    delivery_port: str | None = None
    eta: str | None = None
    rfq_reference: str | None = None
    quotation_reference: str | None = None
    ems_reference: str | None = None
    quote_due_date: str | None = None
    department_or_category: str | None = None
    urgency_markers: list[str] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    """The pipeline's verdict - what both the API and the Outlook handler receive."""

    category: EmailCategory
    direction: Direction
    requires_action: bool
    is_rfq: bool
    recommended_action: RecommendedAction
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool
    priority: Priority = Priority.NORMAL
    decision_path: DecisionPath
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)
    extracted: Signals = Field(default_factory=Signals)
    rule_hits: list[str] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)


class Hints(BaseModel):
    """Everything the deterministic layer knows, handed to the prompt as evidence.

    Computed from the newest message only: run over the full thread, a marker
    found in quoted history would be reported as if it were in this message.
    """

    sender_class: SenderClass = SenderClass.UNKNOWN
    portal: str | None = None
    recipients_are_internal_only: bool = False
    subject_prefixes: list[str] = Field(default_factory=list)
    attachment_kinds: list[AttachmentKind] = Field(default_factory=list)
    template_marker_hits: list[str] = Field(default_factory=list)
    signals: Signals = Field(default_factory=Signals)
    is_reply: bool = False
    quoted_messages_count: int = 0


class ClassificationOutcome(BaseModel):
    """A finished classification: the verdict, the evidence behind it, what it cost.

    Lives here rather than beside the pipeline because three layers need it and
    `infrastructure` may not import `services`.
    """

    result: ClassificationResult
    thread: SplitThread
    hints: Hints
    # Absent when a hard rule decided and no model was called.
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
