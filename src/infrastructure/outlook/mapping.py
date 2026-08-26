"""A Graph message becomes the one shape the pipeline understands.

The mapper lives beside its source, not in the domain: `NormalizedEmail` must
not learn what Graph calls things. The HTTP endpoint has its own mapper for the
same reason, and both produce the identical type.
"""

from src.domain.models import Attachment, EmailAddress, NormalizedEmail
from src.domain.preprocessing.raw_email import body_to_text
from src.domain.preprocessing.signals import classify_attachment
from src.infrastructure.outlook.schemas import (
    Attachment as GraphAttachment,
)
from src.infrastructure.outlook.schemas import (
    EmailMessage,
    MessageBody,
    Recipient,
)


def to_normalized_email(message: EmailMessage, *, mailbox: str | None = None) -> NormalizedEmail:
    """Everything the classifier needs, and nothing Graph-specific."""
    return NormalizedEmail(
        message_id=message.id,
        received_at=message.received_at,
        mailbox=mailbox,
        sender=_address(message.sender),
        to=[_address(item) for item in message.to_recipients],
        cc=[_address(item) for item in message.cc_recipients],
        subject=message.subject,
        body_text=_body_text(message.body),
        attachments=[_attachment(item) for item in message.attachments if item.name],
    )


def _address(recipient: Recipient | None) -> EmailAddress | None:
    if recipient is None:
        return None
    return EmailAddress(
        name=recipient.email_address.name,
        address=recipient.email_address.address,
    )


def _body_text(body: MessageBody | None) -> str:
    """Graph sends HTML for most mail; `body_to_text` handles both shapes."""
    if body is None or not body.content:
        return ""
    if body.is_html:
        return body_to_text(None, body.content)
    return body_to_text(body.content, None)


def _attachment(item: GraphAttachment) -> Attachment:
    """Typed here so the mailbox path gets the same signals as the HTTP one."""
    return Attachment(
        filename=item.name or "",
        content_type=item.content_type,
        size_bytes=item.size,
        kind=classify_attachment(item.name or "", item.size),
    )
