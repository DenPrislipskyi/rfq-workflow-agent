"""The requests this service actually sends to Microsoft Graph.

Nothing here reaches the network. What is pinned is the shape of the payload,
because a wrong shape does not fail a unit test elsewhere - it fails once, in
production, as a 400 from Graph.
"""

from base64 import b64decode
from typing import Any

import pytest

from src.infrastructure.outlook.exceptions import GraphAPIError
from src.infrastructure.outlook.mailbox import (
    ATTACHMENT_FIELDS,
    INLINE_ATTACHMENT_LIMIT,
    MESSAGE_FIELDS,
    Mailbox,
    OutgoingFile,
)

MAILBOX = "supply@our-company.com"
MESSAGE_ID = "AAMkAGNjYzI0NDU4"
DRAFT_ID = "AAMkDRAFT-1"
UPLOAD_URL = "https://upload.example.invalid/session?token=abc"

SMALL = OutgoingFile("KASS RFQ.xlsm", b"a small workbook", "application/x-test")
LARGE = OutgoingFile("KASS RFQ.xlsm", b"x" * (INLINE_ATTACHMENT_LIMIT + 1), "application/x-test")


class FakeGraph:
    """Records every call and answers with whatever the test set up."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}
        self.calls: list[tuple[str, str, Any]] = []
        self.uploaded: list[tuple[str, int]] = []
        # Path fragment -> the exception to raise when a call touches it.
        self.refuse: dict[str, Exception] = {}
        self.delete_fails = False
        # How many of the next PATCHes come back as a change-key conflict,
        # which is what Exchange answers after our own forward has stamped the
        # message we are about to label.
        self.refuse_patches = 0

    async def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, params))
        return self.payload

    async def post(self, path: str, payload: dict | None = None) -> dict:
        self.calls.append(("POST", path, payload))
        self._maybe_refuse(path)
        if path.endswith("/createForward"):
            return {"id": DRAFT_ID}
        if path.endswith("/createUploadSession"):
            return {"uploadUrl": UPLOAD_URL}
        return {}

    async def patch(self, path: str, payload: dict) -> dict:
        self.calls.append(("PATCH", path, payload))
        if self.refuse_patches:
            self.refuse_patches -= 1
            raise GraphAPIError(412, '{"error":{"code":"ErrorIrresolvableConflict"}}')
        return {}

    async def delete(self, path: str) -> None:
        self.calls.append(("DELETE", path, None))
        if self.delete_fails:
            raise GraphAPIError(404, "gone")

    async def upload(self, url: str, data: bytes, *, timeout_s: float | None = None) -> None:
        self.uploaded.append((url, len(data)))

    def _maybe_refuse(self, path: str) -> None:
        for fragment, error in self.refuse.items():
            if fragment in path:
                raise error

    def paths(self, method: str = "POST") -> list[str]:
        return [path for verb, path, _ in self.calls if verb == method]

    def payload_of(self, fragment: str) -> Any:
        return next(body for _, path, body in self.calls if path.endswith(fragment))


def mailbox(payload: dict[str, Any] | None = None) -> tuple[Mailbox, FakeGraph]:
    graph = FakeGraph(payload)
    return Mailbox(graph, MAILBOX), graph  # ty: ignore


# --------------------------------------------------------------------------- #
# Forwarding
# --------------------------------------------------------------------------- #


async def test_recipients_go_inside_the_message_object() -> None:
    """Graph answers 400 when `toRecipients` appears at both levels at once.

    The single-call forward therefore carries every recipient inside `message`
    and nothing beside it.
    """
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid")

    _, path, payload = graph.calls[0]
    assert path == f"/users/{MAILBOX}/messages/{MESSAGE_ID}/forward"
    assert "toRecipients" not in payload
    assert payload["message"]["toRecipients"] == [
        {"emailAddress": {"address": "desk@example.invalid"}}
    ]


async def test_the_forward_adds_no_text_of_its_own() -> None:
    """The desk reads the customer's email as it arrived, with nothing above it."""
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid")

    assert graph.calls[0][2]["comment"] == ""


async def test_a_copy_list_becomes_cc_recipients() -> None:
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid", cc=["a@x.invalid", "b@x.invalid"])

    assert graph.calls[0][2]["message"]["ccRecipients"] == [
        {"emailAddress": {"address": "a@x.invalid"}},
        {"emailAddress": {"address": "b@x.invalid"}},
    ]


async def test_no_copy_list_means_the_key_is_absent() -> None:
    """An empty `ccRecipients` array is a shape Graph need never see."""
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid")

    assert "ccRecipients" not in graph.calls[0][2]["message"]


# --------------------------------------------------------------------------- #
# Reading and labelling
# --------------------------------------------------------------------------- #


async def test_the_message_request_asks_for_what_the_agent_needs() -> None:
    """`body` for the thread, `categories` for the already-handled check."""
    box, graph = mailbox({"id": MESSAGE_ID})

    await box.get_message(MESSAGE_ID)

    assert graph.calls[0][2] == {"$select": MESSAGE_FIELDS}
    assert "body" in MESSAGE_FIELDS and "bodyPreview" not in MESSAGE_FIELDS
    assert "categories" in MESSAGE_FIELDS
    assert "size" in ATTACHMENT_FIELDS


async def test_attachments_are_only_fetched_when_there_are_any() -> None:
    box, graph = mailbox({"id": MESSAGE_ID, "hasAttachments": False})

    await box.get_message(MESSAGE_ID)

    assert len(graph.calls) == 1


async def test_labelling_patches_the_message() -> None:
    box, graph = mailbox()

    await box.set_categories(MESSAGE_ID, ["SSG RFQ"])

    method, path, payload = graph.calls[0]
    assert (method, path) == ("PATCH", f"/users/{MAILBOX}/messages/{MESSAGE_ID}")
    assert payload == {"categories": ["SSG RFQ"]}


async def test_a_label_refused_as_a_conflict_is_written_against_a_fresh_id() -> None:
    """Measured in a live run: `412 ErrorIrresolvableConflict`. The default
    Graph message id carries the item's change key inside it, a write checks
    that key, and our own forward moves it - Exchange stamps the original as
    forwarded. A read does not check it, so reading the message back gives the
    id a write will accept."""
    box, graph = mailbox({"id": "AAMkAGNjYzI0NDU4-NEW-CHANGE-KEY"})
    graph.refuse_patches = 1

    await box.set_categories(MESSAGE_ID, ["SSG RFQ"])

    assert [verb for verb, _, _ in graph.calls] == ["PATCH", "GET", "PATCH"]
    assert graph.calls[-1][1].endswith("AAMkAGNjYzI0NDU4-NEW-CHANGE-KEY")
    assert graph.calls[-1][2] == {"categories": ["SSG RFQ"]}


async def test_a_label_refused_twice_is_reported_rather_than_retried_forever() -> None:
    """One retry, not a loop. Something else is moving the message, and the
    caller has to hear about it: this label is the dedupe that stops a second
    forward."""
    box, graph = mailbox({"id": MESSAGE_ID})
    graph.refuse_patches = 2

    with pytest.raises(GraphAPIError):
        await box.set_categories(MESSAGE_ID, ["SSG RFQ"])


async def test_a_label_refused_for_any_other_reason_is_not_retried() -> None:
    """A 403 is a permission that will not appear by reading the message again."""
    box, graph = mailbox()

    async def forbidden(path: str, payload: dict) -> dict:
        graph.calls.append(("PATCH", path, payload))
        raise GraphAPIError(403, "no MailboxSettings.ReadWrite")

    graph.patch = forbidden  # ty: ignore

    with pytest.raises(GraphAPIError):
        await box.set_categories(MESSAGE_ID, ["SSG RFQ"])

    assert [verb for verb, _, _ in graph.calls] == ["PATCH"]


# --------------------------------------------------------------------------- #
# Forwarding with a file attached
# --------------------------------------------------------------------------- #


async def test_a_forward_carrying_a_file_is_drafted_then_sent() -> None:
    """`/forward` sends immediately, leaving no moment to attach anything.

    Three calls instead of one, in this order, or the desk gets an email with
    nothing on it - or nothing at all.
    """
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid", attachment=SMALL)

    assert graph.paths() == [
        f"/users/{MAILBOX}/messages/{MESSAGE_ID}/createForward",
        f"/users/{MAILBOX}/messages/{DRAFT_ID}/attachments",
        f"/users/{MAILBOX}/messages/{DRAFT_ID}/send",
    ]


async def test_the_plain_forward_is_still_one_call_when_there_is_nothing_to_add() -> None:
    """No draft, no cleanup, no `Mail.ReadWrite` - Graph builds the whole thing."""
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid")

    assert graph.paths() == [f"/users/{MAILBOX}/messages/{MESSAGE_ID}/forward"]


async def test_the_recipients_are_set_on_the_draft_not_after_it() -> None:
    box, graph = mailbox()

    await box.forward(
        MESSAGE_ID, to="desk@example.invalid", cc=["watcher@example.invalid"], attachment=SMALL
    )

    message = graph.payload_of("/createForward")["message"]
    assert message["toRecipients"] == [{"emailAddress": {"address": "desk@example.invalid"}}]
    assert message["ccRecipients"] == [{"emailAddress": {"address": "watcher@example.invalid"}}]


async def test_the_note_rides_along_as_a_comment() -> None:
    """`comment` and `message.body` are mutually exclusive - Graph answers 400
    to a request that sets both - and a body would replace the customer's email
    rather than sit above it."""
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid", comment="Form attached.",
                      attachment=SMALL)

    payload = graph.payload_of("/createForward")
    assert payload["comment"] == "Form attached."
    assert "body" not in payload["message"]


async def test_a_small_file_goes_straight_onto_the_draft() -> None:
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid", attachment=SMALL)

    payload = graph.payload_of("/attachments")
    assert payload["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert payload["name"] == "KASS RFQ.xlsm"
    assert b64decode(payload["contentBytes"]) == SMALL.data
    assert graph.uploaded == []


async def test_a_file_too_big_to_inline_goes_through_an_upload_session() -> None:
    """Graph refuses a posted attachment above 3 MB, and it counts the base64
    expansion, so the real ceiling on the file is about 2.25 MB."""
    box, graph = mailbox()

    await box.forward(MESSAGE_ID, to="desk@example.invalid", attachment=LARGE)

    session = graph.payload_of("/createUploadSession")["AttachmentItem"]
    assert session == {
        "attachmentType": "file",
        "name": "KASS RFQ.xlsm",
        "size": LARGE.size_bytes,
        "contentType": "application/x-test",
    }
    assert graph.uploaded == [(UPLOAD_URL, LARGE.size_bytes)]


async def test_a_draft_that_cannot_be_sent_is_not_left_in_the_drafts_folder() -> None:
    """Otherwise every retry leaves another half-built forward behind."""
    box, graph = mailbox()
    graph.refuse = {"/send": GraphAPIError(413, "too large")}

    with pytest.raises(GraphAPIError):
        await box.forward(MESSAGE_ID, to="desk@example.invalid", attachment=SMALL)

    assert graph.paths("DELETE") == [f"/users/{MAILBOX}/messages/{DRAFT_ID}"]


async def test_a_file_that_will_not_attach_takes_the_draft_with_it() -> None:
    box, graph = mailbox()
    graph.refuse = {"/attachments": GraphAPIError(400, "no")}

    with pytest.raises(GraphAPIError):
        await box.forward(MESSAGE_ID, to="desk@example.invalid", attachment=SMALL)

    assert graph.paths("DELETE") == [f"/users/{MAILBOX}/messages/{DRAFT_ID}"]
    assert f"/users/{MAILBOX}/messages/{DRAFT_ID}/send" not in graph.paths()


async def test_a_draft_that_will_not_delete_does_not_hide_the_real_failure() -> None:
    """The caller is already handling the failure that brought us here."""
    box, graph = mailbox()
    graph.refuse = {"/send": GraphAPIError(413, "too large")}
    graph.delete_fails = True

    with pytest.raises(GraphAPIError) as raised:
        await box.forward(MESSAGE_ID, to="desk@example.invalid", attachment=SMALL)

    assert raised.value.status_code == 413
