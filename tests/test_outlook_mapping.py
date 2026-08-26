"""A Graph message becomes a NormalizedEmail.

The payloads here are shaped the way Graph really answers, so the test fails if
`MESSAGE_FIELDS` ever stops asking for something the classifier needs.
"""

from src.domain.enums import AttachmentKind
from src.infrastructure.outlook.mapping import to_normalized_email
from src.infrastructure.outlook.schemas import EmailMessage

GRAPH_PAYLOAD = {
    "id": "AAMkAGNjYzI0NDU4",
    "subject": "[Updated]VSL: NORTH STAR, QUOTATION: 0015-AB000001C",
    "receivedDateTime": "2026-08-26T09:49:21Z",
    "from": {"emailAddress": {"name": "NEW COMPANY", "address": "purchasing@new-company.com"}},
    "toRecipients": [
        {"emailAddress": {"name": "OUR COMPANY (UAE)", "address": "supply@our-company.com"}}
    ],
    "ccRecipients": [{"emailAddress": {"address": "michael.reed@our-company.com"}}],
    "hasAttachments": True,
    "body": {
        "contentType": "html",
        "content": "<html><body><p>you may find attached our RFQ</p>"
        "<p>quotation due date is 02/June/2026</p></body></html>",
    },
    "webLink": "https://outlook.office365.com/...",
}


def message(**overrides) -> EmailMessage:
    return EmailMessage.model_validate(GRAPH_PAYLOAD | overrides)


def test_every_field_the_classifier_needs_survives_the_mapping() -> None:
    email = to_normalized_email(message(), mailbox="supply@our-company.com")

    assert email.message_id == "AAMkAGNjYzI0NDU4"
    assert email.subject is not None and email.subject.startswith("[Updated]")
    assert email.sender is not None and email.sender.address == "purchasing@new-company.com"
    assert [item.address for item in email.to] == ["supply@our-company.com"]
    assert [item.address for item in email.cc] == ["michael.reed@our-company.com"]
    assert email.mailbox == "supply@our-company.com"
    assert email.received_at is not None


def test_cc_is_carried_over_because_a_hard_rule_reads_it() -> None:
    """R2 only fires when every recipient is internal - it has to see cc."""
    email = to_normalized_email(message())
    assert len(email.cc) == 1


def test_an_html_body_arrives_as_plain_text() -> None:
    """Graph sends HTML for most mail; the pipeline only ever reads text."""
    email = to_normalized_email(message())

    assert "<p>" not in email.body_text
    assert "you may find attached our RFQ" in email.body_text
    assert "quotation due date is 02/June/2026" in email.body_text


def test_a_plain_text_body_is_left_as_it_is() -> None:
    email = to_normalized_email(
        message(body={"contentType": "text", "content": "please quote the attached list"})
    )
    assert email.body_text == "please quote the attached list"


def test_a_message_without_a_body_maps_to_an_empty_one() -> None:
    email = to_normalized_email(message(body=None))
    assert email.body_text == ""


def test_attachments_are_typed_the_same_way_as_over_http() -> None:
    email = to_normalized_email(
        message(
            attachments=[
                {"name": "E_QUOT_XLS_0015.XLSX", "size": 40311},
                {"name": "image001.png", "size": 4000},
            ]
        )
    )
    kinds = {item.filename: item.kind for item in email.attachments}

    assert kinds["E_QUOT_XLS_0015.XLSX"] is AttachmentKind.RFQ_FORM_XLSX
    assert kinds["image001.png"] is AttachmentKind.SIGNATURE_IMAGE


def test_an_attachment_without_a_name_is_dropped() -> None:
    """Nothing downstream can do anything with a nameless file in Phase 1."""
    email = to_normalized_email(message(attachments=[{"size": 100}]))
    assert email.attachments == []


def test_a_bare_message_maps_without_raising() -> None:
    """Graph omits whatever is empty; the mapper must not assume any field."""
    email = to_normalized_email(EmailMessage.model_validate({"id": "x"}))

    assert email.message_id == "x"
    assert email.sender is None
    assert email.to == []
    assert email.body_text == ""
