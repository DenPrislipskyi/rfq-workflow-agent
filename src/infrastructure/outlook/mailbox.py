import logging
from base64 import b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from src.infrastructure.outlook.client import GraphClient
from src.infrastructure.outlook.exceptions import GraphAPIError
from src.infrastructure.outlook.schemas import Attachment, EmailMessage

logger = logging.getLogger(__name__)

# What Exchange answers when the change key inside the id we wrote against has
# moved on. 409 is the same complaint from the endpoints that phrase it that way.
_CONFLICT = frozenset({409, 412})

# `body`, not `bodyPreview`: the preview is a ~255 character snippet, and a
# classifier reading only that never sees the quoted thread below it.
# `categories` is what lets the handler recognise a message it already finished:
# Graph re-sends notifications, and a forward sent twice is two real emails.
MESSAGE_FIELDS = (
    "id,subject,receivedDateTime,from,toRecipients,ccRecipients,"
    "hasAttachments,body,webLink,categories"
)
# Size is what separates a signature logo from a real attachment, and `isInline`
# confirms it. `contentBytes` is deliberately absent: Graph inlines it into the
# JSON when nothing is selected, which turns a listing of four attachments into
# a 30 MB response. The bytes are fetched one at a time from `/$value` instead.
ATTACHMENT_FIELDS = "id,name,size,contentType,isInline"

# Above this Graph refuses a file posted inline and wants an upload session.
# The documented boundary is 3 MB, and it counts the base64 expansion, so the
# threshold is on the encoded size rather than on the file's own.
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024

# Uploading 12 MB in four chunks over a slow link outlasts the timeout the rest
# of the service is happy with.
UPLOAD_TIMEOUT_S = 300.0


@dataclass(frozen=True, slots=True)
class OutgoingFile:
    """A file to attach to a message we are about to send."""

    filename: str
    data: bytes
    content_type: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


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

        Written twice when the first one is refused as a conflict, because the
        default Graph message id **carries the item's change key inside it** and
        a write checks that key. Our own forward invalidates it: sending it
        makes Exchange stamp the original as forwarded, the key moves, and the
        label we send a moment later comes back
        `412 ErrorIrresolvableConflict`.

        Measured: the label used to go out ~150 ms after the send and won the
        race; the run where it went out 7 seconds after lost it. A read does
        not check the key, so reading the message back hands us the current id
        to write against.
        """
        try:
            await self._patch_categories(message_id, categories)
        except GraphAPIError as error:
            if error.status_code not in _CONFLICT:
                raise
            logger.info(
                "The label on %s was refused as a conflict, reading it again", message_id
            )
            await self._patch_categories(await self._current_id(message_id), categories)

    async def _patch_categories(self, message_id: str, categories: list[str]) -> None:
        await self._client.patch(
            f"{self._root}/messages/{message_id}", {"categories": categories}
        )

    async def _current_id(self, message_id: str) -> str:
        """The id the item has now, change key included.

        The one we were given still finds it - reads do not check the key - and
        what comes back is what a write will accept.
        """
        payload = await self._client.get(
            f"{self._root}/messages/{message_id}", params={"$select": "id"}
        )
        return payload.get("id") or message_id

    async def forward(
        self,
        message_id: str,
        *,
        to: str,
        cc: Sequence[str] = (),
        comment: str = "",
        attachment: OutgoingFile | None = None,
    ) -> None:
        """Forward the message as the mailbox itself, attachments included.

        Two routes, because Graph offers no single call that does both. With
        nothing new to carry, `/forward` builds and sends the whole thing
        server-side in one request. With a file to add, the forward has to exist
        as a draft first - see `_forward_carrying`.

        Recipients go inside `message`, never beside it: Graph answers 400 to a
        request carrying `toRecipients` in both places. Sending needs `Mail.Send`.
        """
        if attachment is None:
            # The empty comment is deliberate: the recipient sees the customer's
            # email exactly as it arrived, with nothing written above it. Graph
            # documents "" as a valid value, so the key stays rather than
            # vanishing.
            await self._client.post(
                f"{self._root}/messages/{message_id}/forward",
                {"comment": comment, "message": self._envelope(to, cc)},
            )
            return

        await self._forward_carrying(message_id, to, cc, comment, attachment)

    async def _forward_carrying(
        self,
        message_id: str,
        to: str,
        cc: Sequence[str],
        comment: str,
        attachment: OutgoingFile,
    ) -> None:
        """Draft the forward, attach the file, send it.

        `/forward` cannot carry a new attachment - it sends immediately, with no
        moment in between to add one - so the same forward is built in three
        calls instead. `comment` goes in at the first step rather than by
        patching the body afterwards: Graph rejects a request that sets both,
        and `message.body` would replace the customer's email rather than sit
        above it.

        Needs `Mail.ReadWrite` on top of `Mail.Send`: the middle step writes to
        a draft in this mailbox.
        """
        draft = await self._client.post(
            f"{self._root}/messages/{message_id}/createForward",
            {"comment": comment, "message": self._envelope(to, cc)},
        )
        draft_id = draft["id"]

        try:
            await self._attach(draft_id, attachment)
            await self._client.post(f"{self._root}/messages/{draft_id}/send")
        except Exception:
            # Otherwise every retry leaves another half-built forward in Drafts
            # for somebody to find and wonder about.
            await self._discard(draft_id)
            raise

    async def _attach(self, draft_id: str, file: OutgoingFile) -> None:
        """Put the file on the draft, by whichever route its size allows."""
        encoded = b64encode(file.data)

        if len(encoded) < INLINE_ATTACHMENT_LIMIT:
            await self._client.post(
                f"{self._root}/messages/{draft_id}/attachments",
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": file.filename,
                    "contentType": file.content_type,
                    "contentBytes": encoded.decode("ascii"),
                },
            )
            return

        session = await self._client.post(
            f"{self._root}/messages/{draft_id}/attachments/createUploadSession",
            {
                "AttachmentItem": {
                    "attachmentType": "file",
                    "name": file.filename,
                    "size": file.size_bytes,
                    "contentType": file.content_type,
                }
            },
        )
        logger.info(
            "Uploading %s (%.1f MB) to draft %s",
            file.filename,
            file.size_bytes / (1024 * 1024),
            draft_id,
        )
        await self._client.upload(
            session["uploadUrl"], file.data, timeout_s=UPLOAD_TIMEOUT_S
        )

    async def _discard(self, draft_id: str) -> None:
        """Delete a draft that will never be sent. Never raises: the caller is
        already handling the failure that brought us here."""
        try:
            await self._client.delete(f"{self._root}/messages/{draft_id}")
        except Exception as error:
            logger.warning("Left an unsent draft %s behind: %s", draft_id, error)

    def _envelope(self, to: str, cc: Sequence[str]) -> dict[str, Any]:
        message: dict[str, Any] = {"toRecipients": [_recipient(to)]}
        if cc:
            message["ccRecipients"] = [_recipient(address) for address in cc]
        return message

    async def get_attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        """The attachment itself, whatever kind it is.

        `/$value` streams the raw bytes, so nothing base64-decodes a 20 MB
        attachment out of a JSON string. For an **item** attachment - an email
        somebody forwarded as a file - the same route returns the message as
        raw MIME, which is better than what `$expand` gives: `$expand` returns
        the message as structure and leaves its own attachments behind, while
        the MIME carries them and stage A unpacks it like any other `.eml`.

        A reference attachment is the one kind with no bytes anywhere: it is a
        OneDrive link, and the caller does not ask.
        """
        return await self._client.get_bytes(
            f"{self._root}/messages/{message_id}/attachments/{attachment_id}/$value"
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
