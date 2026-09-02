from collections.abc import Sequence
from typing import Any

from src.infrastructure.outlook.client import GraphClient
from src.infrastructure.outlook.schemas import Attachment, EmailMessage

# `body`, not `bodyPreview`: the preview is a ~255 character snippet, and a
# classifier reading only that never sees the quoted thread below it.
# `categories` is what lets the handler recognise a message it already finished:
# Graph re-sends notifications, and a forward sent twice is two real emails.
MESSAGE_FIELDS = (
    "id,subject,receivedDateTime,from,toRecipients,ccRecipients,"
    "hasAttachments,body,webLink,categories"
)
# Size is what separates a signature logo from a real attachment.
ATTACHMENT_FIELDS = "name,size,contentType"


class Mailbox:
    """One mailbox, read and write. The only place that builds mailbox paths."""

    def __init__(self, client: GraphClient, mailbox_address: str) -> None:
        self._client = client
        self._address = mailbox_address
        self._root = f"/users/{mailbox_address}"

    @property
    def address(self) -> str:
        return self._address

    @property
    def inbox_resource(self) -> str:
        """Resource path a change subscription has to point at."""
        return f"{self._root}/mailFolders('inbox')/messages"

    async def get_message(self, message_id: str) -> EmailMessage:
        payload = await self._client.get(
            f"{self._root}/messages/{message_id}",
            params={"$select": MESSAGE_FIELDS},
        )
        message = EmailMessage.model_validate(payload)

        if message.has_attachments:
            message.attachments = await self._get_attachments(message_id)

        return message

    async def set_categories(self, message_id: str, categories: list[str]) -> None:
        """Stamp the mailbox copy so the decision is visible in Outlook itself.

        The names must already exist in the mailbox for the colour to show:
        creating them needs `MailboxSettings.ReadWrite`, which this app does not
        request, so an operator adds them once by hand.
        """
        await self._client.patch(
            f"{self._root}/messages/{message_id}", {"categories": categories}
        )

    async def forward(self, message_id: str, *, to: str, cc: Sequence[str] = ()) -> None:
        """Forward the message as the mailbox itself, attachments included.

        Graph builds the forward server-side, so nothing is re-composed here and
        the copy lands in this mailbox's Sent Items. Recipients go inside
        `message`, never beside it: Graph answers 400 to a request carrying
        `toRecipients` in both places. Needs `Mail.Send`.
        """
        message: dict[str, Any] = {"toRecipients": [_recipient(to)]}
        if cc:
            message["ccRecipients"] = [_recipient(address) for address in cc]

        # The empty comment is deliberate: the recipient sees the customer's
        # email exactly as it arrived, with nothing written above it. Graph
        # documents "" as a valid value, so the key stays rather than vanishing.
        await self._client.post(
            f"{self._root}/messages/{message_id}/forward",
            {"comment": "", "message": message},
        )

    async def _get_attachments(self, message_id: str) -> list[Attachment]:
        """A second call: Graph does not return attachment metadata with the message."""
        payload = await self._client.get(
            f"{self._root}/messages/{message_id}/attachments",
            params={"$select": ATTACHMENT_FIELDS},
        )
        return [Attachment.model_validate(item) for item in payload.get("value", [])]


def _recipient(address: str) -> dict[str, Any]:
    return {"emailAddress": {"address": address}}
