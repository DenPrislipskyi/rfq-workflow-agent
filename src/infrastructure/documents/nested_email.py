"""An email attached to an email, unpacked one level deep.

This is how a forwarded RFQ usually reaches a shared mailbox: someone at the
customer forwards the original as an attachment rather than inline, so the
requisition spreadsheet is two levels down and invisible to a reader that only
looks at the outer message.

`.eml` is RFC 5322 and the standard library handles it. `.msg` is Outlook's own
OLE format and needs extract-msg.
"""

import io
import logging
from email import message_from_bytes
from email.message import Message

import extract_msg

from src.domain.preprocessing.html import html_to_text
from src.infrastructure.documents.models import UNREADABLE

logger = logging.getLogger(__name__)


def unpack_eml(data: bytes) -> tuple[str, list[tuple[str, bytes]], list[str]]:
    """Return the body text, the files hanging off it, and any warning codes."""
    try:
        message = message_from_bytes(data)
    except Exception as error:  # noqa: BLE001
        logger.debug("Could not parse .eml: %s", error)
        return "", [], [UNREADABLE]

    return _body(message), _attachments(message), []


def unpack_msg(data: bytes) -> tuple[str, list[tuple[str, bytes]], list[str]]:
    try:
        with extract_msg.Message(io.BytesIO(data)) as message:
            body = (message.body or "").strip()
            files = [
                (attachment.longFilename or attachment.shortFilename or "attachment", payload)
                for attachment in message.attachments
                if isinstance(payload := attachment.data, bytes)
            ]
    except Exception as error:  # noqa: BLE001 - extract-msg raises its own hierarchy
        logger.debug("Could not parse .msg: %s", error)
        return "", [], [UNREADABLE]

    return body, files, []


def _body(message: Message) -> str:
    """The plain-text part, or the HTML one stripped down if that is all there is."""
    html: str | None = None

    for part in message.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        content = _decoded(part)
        if part.get_content_subtype() == "plain":
            return content.strip()
        if part.get_content_subtype() == "html" and html is None:
            html = content

    return html_to_text(html).strip() if html else ""


def _attachments(message: Message) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename or part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload:
            files.append((filename, payload))
    return files


def _decoded(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")
