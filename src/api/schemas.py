"""The HTTP contract for classification.

Kept separate from the internal models on purpose: those are free to change
shape as the pipeline grows, while what a caller sees stays stable and versioned
by `schema_version`.
"""

from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field, model_validator

from src.domain.enums import (
    AttachmentKind,
    DecisionPath,
    Direction,
    EmailCategory,
    Priority,
    RecommendedAction,
)
from src.domain.models import (
    Attachment,
    ClassificationOutcome,
    EmailAddress,
    NormalizedEmail,
)
from src.domain.preprocessing.raw_email import body_to_text, parse_raw_email
from src.domain.preprocessing.signals import classify_attachment

SCHEMA_VERSION = "1.0"


class AddressIn(BaseModel):
    name: str | None = None
    address: str | None = None


class AttachmentIn(BaseModel):
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None


class ClassifyEmailRequest(BaseModel):
    """One email in either of two shapes.

    `raw_text` is the dump a person pastes out of Outlook; the structured fields
    are what a backend already holding a parsed message sends, and they do
    better because nothing has to be recovered by regex.
    """

    raw_text: str | None = None

    message_id: str | None = None
    conversation_id: str | None = None
    received_at: datetime | None = None
    mailbox: str | None = None
    region_hint: str | None = None
    sender: AddressIn | None = None
    to: list[AddressIn] = Field(default_factory=list)
    cc: list[AddressIn] = Field(default_factory=list)
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[AttachmentIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_of_the_two_modes_must_be_complete(self) -> Self:
        if self._is_raw or (self.subject and (self.body_text or self.body_html)):
            return self
        raise ValueError(
            "send either raw_text, or subject together with body_text or body_html"
        )

    def to_domain(self) -> NormalizedEmail:
        """One shape for the pipeline, whichever mode the caller used."""
        if self._is_raw:
            email = parse_raw_email(self.raw_text or "")
        else:
            email = NormalizedEmail(
                sender=_address(self.sender) if self.sender else None,
                to=[_address(item) for item in self.to],
                cc=[_address(item) for item in self.cc],
                subject=self.subject,
                body_text=body_to_text(self.body_text, self.body_html),
                attachments=[_attachment(item) for item in self.attachments],
            )

        # The envelope always comes from the caller: a raw dump does not carry
        # it, and a backend knows it better than any parser could.
        return email.model_copy(
            update={
                "message_id": self.message_id,
                "mailbox": self.mailbox,
                "region_hint": self.region_hint,
                "received_at": self.received_at,
            }
        )

    @property
    def _is_raw(self) -> bool:
        return bool(self.raw_text and self.raw_text.strip())


class ThreadInfo(BaseModel):
    is_reply: bool
    quoted_messages_count: int
    latest_message_chars: int


class ExtractedOut(BaseModel):
    """What the deterministic layer found in the newest message.

    Regex only, so nothing here can be a hallucination; richer extraction is
    Phase 2.
    """

    customer_domain: str | None = None
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
    attachment_kinds: list[AttachmentKind] = Field(default_factory=list)


class Meta(BaseModel):
    """How the answer was produced. `model` is null when a hard rule decided."""

    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    prompt_version: str


class ClassifyEmailResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    # Null when journalling is off - there is no line on disk to point at.
    decision_id: str | None = None
    message_id: str | None = None
    requires_action: bool
    is_rfq: bool
    category: EmailCategory
    recommended_action: RecommendedAction
    direction: Direction
    confidence: float
    needs_human_review: bool
    priority_hint: Priority
    reasoning: str
    evidence: list[str]
    extracted: ExtractedOut
    thread: ThreadInfo
    rule_hits: list[str]
    decision_path: DecisionPath
    parse_warnings: list[str]
    meta: Meta

    @classmethod
    def from_outcome(
        cls,
        outcome: ClassificationOutcome,
        email: NormalizedEmail,
        prompt_version: str,
        decision_id: str | None = None,
    ) -> "ClassifyEmailResponse":
        """Flatten a pipeline outcome into the wire format."""
        result = outcome.result
        return cls(
            decision_id=decision_id,
            message_id=email.message_id,
            requires_action=result.requires_action,
            is_rfq=result.is_rfq,
            category=result.category,
            recommended_action=result.recommended_action,
            direction=result.direction,
            confidence=result.confidence,
            needs_human_review=result.needs_human_review,
            priority_hint=result.priority,
            reasoning=result.reasoning,
            evidence=result.evidence,
            extracted=ExtractedOut(
                customer_domain=email.sender.domain if email.sender else None,
                attachment_kinds=outcome.hints.attachment_kinds,
                **result.extracted.model_dump(),
            ),
            thread=ThreadInfo(
                is_reply=outcome.thread.is_reply,
                quoted_messages_count=len(outcome.thread.quoted_messages),
                latest_message_chars=len(outcome.thread.latest_message),
            ),
            rule_hits=result.rule_hits,
            decision_path=result.decision_path,
            parse_warnings=result.parse_warnings,
            meta=Meta(
                model=outcome.model,
                latency_ms=outcome.latency_ms,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                prompt_version=prompt_version,
            ),
        )


def _address(value: AddressIn) -> EmailAddress:
    return EmailAddress(name=value.name, address=value.address)


def _attachment(value: AttachmentIn) -> Attachment:
    """Type the file here so every entry point gets the same attachment signals."""
    return Attachment(
        filename=value.filename,
        content_type=value.content_type,
        size_bytes=value.size_bytes,
        kind=classify_attachment(value.filename, value.size_bytes),
    )
