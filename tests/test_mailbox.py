"""The requests this service actually sends to Microsoft Graph.

Nothing here reaches the network. What is pinned is the shape of the payload,
because a wrong shape does not fail a unit test elsewhere - it fails once, in
production, as a 400 from Graph.
"""

from typing import Any

from src.infrastructure.outlook.mailbox import ATTACHMENT_FIELDS, MESSAGE_FIELDS, Mailbox

MAILBOX = "supply@our-company.com"
MESSAGE_ID = "AAMkAGNjYzI0NDU4"


class FakeGraph:
    """Records every call and answers with whatever the test set up."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}
        self.calls: list[tuple[str, str, Any]] = []

    async def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, params))
        return self.payload

    async def post(self, path: str, payload: dict) -> dict:
        self.calls.append(("POST", path, payload))
        return {}

    async def patch(self, path: str, payload: dict) -> dict:
        self.calls.append(("PATCH", path, payload))
        return {}


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
