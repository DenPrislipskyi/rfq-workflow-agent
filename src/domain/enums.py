from enum import StrEnum


class EmailCategory(StrEnum):
    """What kind of traffic the email is. The single input to the routing policy."""

    NEW_RFQ = "NEW_RFQ"
    UPDATED_RFQ = "UPDATED_RFQ"
    PORTAL_RFQ_NOTIFICATION = "PORTAL_RFQ_NOTIFICATION"
    CUSTOMER_ORDER_PO = "CUSTOMER_ORDER_PO"
    CUSTOMER_CLARIFICATION = "CUSTOMER_CLARIFICATION"
    SUPPLIER_CORRESPONDENCE = "SUPPLIER_CORRESPONDENCE"
    QUOTE_STATUS_NOTIFICATION = "QUOTE_STATUS_NOTIFICATION"
    INTERNAL = "INTERNAL"
    OUTBOUND_OWN = "OUTBOUND_OWN"
    SPAM_MARKETING = "SPAM_MARKETING"
    AUTO_REPLY_SYSTEM = "AUTO_REPLY_SYSTEM"
    OTHER_NON_ACTIONABLE = "OTHER_NON_ACTIONABLE"
    UNCERTAIN = "UNCERTAIN"


class Direction(StrEnum):
    """Who wrote the newest message, relative to the chandler."""

    INBOUND_CUSTOMER = "INBOUND_CUSTOMER"
    INBOUND_SUPPLIER = "INBOUND_SUPPLIER"
    INBOUND_PORTAL = "INBOUND_PORTAL"
    INTERNAL = "INTERNAL"
    OUTBOUND_OWN = "OUTBOUND_OWN"
    UNKNOWN = "UNKNOWN"


class RecommendedAction(StrEnum):
    """Where the Mailbox Team should send the email."""

    FORWARD_TO_DST = "FORWARD_TO_DST"
    ROUTE_TO_CS = "ROUTE_TO_CS"
    ROUTE_TO_SOURCING = "ROUTE_TO_SOURCING"
    ROUTE_TO_ORDER_TEAM = "ROUTE_TO_ORDER_TEAM"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    IGNORE = "IGNORE"


class DecisionPath(StrEnum):
    """How the decision was reached - for debugging and shadow comparison."""

    RULES_FAST_PATH = "RULES_FAST_PATH"
    LLM = "LLM"
    LLM_WITH_GUARDRAIL_OVERRIDE = "LLM_WITH_GUARDRAIL_OVERRIDE"


class Priority(StrEnum):
    """How urgently the email should be picked up."""

    URGENT = "URGENT"
    NORMAL = "NORMAL"
    LOW = "LOW"


class AttachmentKind(StrEnum):
    """Attachment type, inferred from the filename alone (Phase 1 opens no files).

    Only distinctions that change something: RFQ forms trip the safety net in
    `policy`, and a signature logo is an attachment that is not content.
    Everything else has never altered a decision, so it lands in OTHER.
    """

    RFQ_FORM_XLSX = "RFQ_FORM_XLSX"
    RFQ_FORM_PDF = "RFQ_FORM_PDF"
    SIGNATURE_IMAGE = "SIGNATURE_IMAGE"
    OTHER = "OTHER"


class SenderClass(StrEnum):
    """What the registries say about the sender of the newest message."""

    INTERNAL_OWN = "INTERNAL_OWN"
    INTERNAL_SYSTEM = "INTERNAL_SYSTEM"
    KNOWN_CUSTOMER = "KNOWN_CUSTOMER"
    KNOWN_SUPPLIER = "KNOWN_SUPPLIER"
    PORTAL = "PORTAL"
    EXTERNAL_UNKNOWN = "EXTERNAL_UNKNOWN"
    # No sender at all: raw dumps often arrive without a header block.
    UNKNOWN = "UNKNOWN"
