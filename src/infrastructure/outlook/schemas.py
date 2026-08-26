from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GraphModel(BaseModel):
    """Graph speaks camelCase, the application speaks snake_case."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ResourceData(GraphModel):
    id: str | None = None


class ChangeNotification(GraphModel):
    """A single change notification pushed by Microsoft Graph.

    It carries no email content - only the identity of what has changed.
    """

    subscription_id: str = Field(alias="subscriptionId")
    change_type: str = Field(alias="changeType")
    resource: str
    client_state: str | None = Field(default=None, alias="clientState")
    resource_data: ResourceData | None = Field(default=None, alias="resourceData")

    @property
    def message_id(self) -> str | None:
        return self.resource_data.id if self.resource_data else None


class ChangeNotificationCollection(GraphModel):
    value: list[ChangeNotification] = Field(default_factory=list)


class EmailAddress(GraphModel):
    name: str | None = None
    address: str | None = None


class Recipient(GraphModel):
    email_address: EmailAddress = Field(alias="emailAddress")


class MessageBody(GraphModel):
    """Graph returns HTML for most mail and plain text for some."""

    content: str | None = None
    content_type: str | None = Field(default=None, alias="contentType")

    @property
    def is_html(self) -> bool:
        return (self.content_type or "").lower() == "html"


class Attachment(GraphModel):
    """Name and size are enough: Phase 1 never opens an attachment."""

    name: str | None = None
    size: int | None = None
    content_type: str | None = Field(default=None, alias="contentType")


class EmailMessage(GraphModel):
    """The part of a Graph message the RFQ workflow actually needs."""

    id: str
    subject: str | None = None
    received_at: datetime | None = Field(default=None, alias="receivedDateTime")
    sender: Recipient | None = Field(default=None, alias="from")
    to_recipients: list[Recipient] = Field(default_factory=list, alias="toRecipients")
    cc_recipients: list[Recipient] = Field(default_factory=list, alias="ccRecipients")
    has_attachments: bool = Field(default=False, alias="hasAttachments")
    body: MessageBody | None = None
    web_link: str | None = Field(default=None, alias="webLink")
    attachments: list[Attachment] = Field(default_factory=list)

    @property
    def sender_address(self) -> str | None:
        return self.sender.email_address.address if self.sender else None
