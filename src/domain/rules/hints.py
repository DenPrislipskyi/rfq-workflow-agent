"""L2: soft signals handed to the prompt as evidence, never as a verdict.

Everything here is computed from the newest message. Measured on the corpus,
running the same markers over the full thread misleads in 6 of 13 emails: a
quoted portal notice makes internal chatter look like a portal RFQ.
"""

import re

from src.domain.enums import AttachmentKind, SenderClass
from src.domain.models import Attachment, Hints, NormalizedEmail, SplitThread
from src.domain.preprocessing.signals import classify_attachment, extract_signals
from src.domain.rules.registries import Registries

# "[Updated]" is the only thing separating a resent RFQ from the first
# submission in this corpus.
_SUBJECT_PREFIXES = re.compile(r"^\s*((?:re|fw|fwd)\s*:|\[updated\])", re.IGNORECASE)


def build_hints(
    email: NormalizedEmail, thread: SplitThread, registries: Registries
) -> Hints:
    """Collect every deterministic signal about the newest message."""
    address = email.sender.address if email.sender else None
    domain = email.sender.domain if email.sender else None

    return Hints(
        sender_class=registries.classify_sender(address, domain),
        portal=_find_portal(thread.latest_message, domain, registries),
        recipients_are_internal_only=_recipients_are_internal_only(email, registries),
        subject_prefixes=_subject_prefixes(email.subject),
        attachment_kinds=sorted({_kind_of(item) for item in email.attachments}),
        template_marker_hits=registries.matching_markers(thread.latest_message),
        signals=extract_signals(_subject_and_body(email, thread)),
        is_reply=thread.is_reply,
        quoted_messages_count=len(thread.quoted_messages),
    )


def _subject_and_body(email: NormalizedEmail, thread: SplitThread) -> str:
    """The subject belongs to the newest message, so extraction has to see it.

    Vessel names and quotation references live there far more often than in the
    body: "VSL: NORTH STAR, QUOTATION: 0015-AB000001C".
    """
    if not email.subject:
        return thread.latest_message
    return f"{email.subject}\n{thread.latest_message}"


def _kind_of(attachment: Attachment) -> AttachmentKind:
    """Entry points may hand us unclassified attachments; the filename is enough."""
    if attachment.kind is not AttachmentKind.OTHER:
        return attachment.kind
    return classify_attachment(attachment.filename, attachment.size_bytes)


def _find_portal(text: str, domain: str | None, registries: Registries) -> str | None:
    """The sender domain is the strong signal; the body is the fallback."""
    return registries.find_portal_by_domain(domain) or registries.find_portal_in_text(text)


def _recipients_are_internal_only(email: NormalizedEmail, registries: Registries) -> bool:
    recipients = [*email.to, *email.cc]
    if not recipients:
        return False
    return all(registries.is_internal_domain(person.domain) for person in recipients)


def _subject_prefixes(subject: str | None) -> list[str]:
    """Read the prefixes off the front of a subject: "RE: FW: [Updated]Vessel ..."."""
    if not subject:
        return []

    prefixes: list[str] = []
    rest = subject
    while match := _SUBJECT_PREFIXES.match(rest):
        prefixes.append(match.group(1).strip().upper())
        rest = rest[match.end() :]
    return prefixes


def render_hints(hints: Hints) -> str:
    """Format the hints for the prompt: compact, labelled, easy for a human to read."""
    signals = {key: value for key, value in hints.signals.model_dump().items() if value}
    lines = [
        f"sender_class: {hints.sender_class.value}",
        f"portal: {hints.portal or 'none'}",
        f"recipients_are_internal_only: {str(hints.recipients_are_internal_only).lower()}",
        f"subject_prefixes: {hints.subject_prefixes or 'none'}",
        f"attachment_kinds: {[kind.value for kind in hints.attachment_kinds] or 'none'}",
        f"template_marker_hits: {hints.template_marker_hits or 'none'}",
        f"regex_hits: {signals or 'none'}",
        f"thread: is_reply={str(hints.is_reply).lower()}, "
        f"quoted_messages={hints.quoted_messages_count}",
    ]
    return "\n".join(lines)


# Re-exported so callers need not know where SenderClass lives.
__all__ = ["Hints", "SenderClass", "build_hints", "render_hints"]
