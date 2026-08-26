"""Worked examples, abbreviated from the real corpus.

Each shot stores the parts of a user turn rather than a finished string, so
`prompt.render_user_message` renders shots and live emails through the same
code. Ten of the thirteen corpus emails appear here - only 007, 012 and 013 are
held out, so scores on the rest are contaminated by definition.
"""

from dataclasses import dataclass

from src.domain.enums import Direction, EmailCategory, Priority
from src.services.classification.schemas import LLMClassification


@dataclass(frozen=True, slots=True)
class FewShot:
    """One user turn and the answer the model should have given.

    `source_id` names the corpus email this was abbreviated from, which is how
    the eval harness computes the held-out set instead of hardcoding it.
    """

    source_id: str
    signals: str
    metadata: str
    latest: str
    answer: LLMClassification
    quoted: str = ""


FEW_SHOTS: list[FewShot] = [
    # 1. A customer's first RFQ. The demand lives in the attachment, not the body.
    FewShot(
        source_id="001",
        signals=(
            "sender_class: KNOWN_CUSTOMER\n"
            "attachment_kinds: ['RFQ_FORM_PDF', 'RFQ_FORM_XLSX']\n"
            "regex_hits: {'vessel_name': 'North Star', 'quote_due_date': '02/June/2026', "
            "'department_or_category': 'Engine Materials'}\n"
            "thread: is_reply=false, quoted_messages=0"
        ),
        metadata=(
            "from: purchasing@new-company.com\n"
            "to: OUR COMPANY (UAE) <supply@our-company.com>\n"
            "subject: VSL: NORTH STAR, QUOTATION: 0015-AB000001C, VENDOR: OUR COMPANY (UAE)\n"
            "mailbox: supply@our-company.com\n"
            "attachments: E_QUOT_0015-AB000001C_S001-01.pdf; "
            "E_QUOT_XLS_0015-AB000001C_S001-01.XLSX"
        ),
        latest=(
            "Dear Sir/Madam\n"
            'you may find attached our RFQ for Department "Engine Materials".\n'
            "For your quotation, insert prices, discount, select currency and remarks as well "
            "as your offer reference number, you must use our excel form's cells, color yellow.\n"
            "m/t North Star - eta Fujairah subject to Hormuz straight opening\n"
            "Please consider quotation due date is 02/June/2026."
        ),
        answer=LLMClassification(
            category=EmailCategory.NEW_RFQ,
            direction=Direction.INBOUND_CUSTOMER,
            is_rfq=True,
            confidence=0.95,
            reasoning=(
                "An external shipping company's purchasing department asks the chandler to price an "
                "attached RFQ form for a named vessel with a stated quotation due date. "
                "Nothing in the thread precedes it, so this is a first submission."
            ),
            evidence=[
                "sender domain new-company.com is an external shipping company",
                "'you may find attached our RFQ for Department \"Engine Materials\"'",
                "attachment E_QUOT_XLS_0015-AB000001C_S001-01.XLSX is an RFQ form",
                "'quotation due date is 02/June/2026'",
                "no quoted history",
            ],
        ),
    ),
    # 2. The same body resent. Only the subject prefix separates it from shot 1.
    FewShot(
        source_id="002",
        signals=(
            "sender_class: KNOWN_CUSTOMER\n"
            "subject_prefixes: ['[UPDATED]']\n"
            "attachment_kinds: ['RFQ_FORM_PDF', 'RFQ_FORM_XLSX']\n"
            "regex_hits: {'vessel_name': 'North Star', 'quote_due_date': '02/June/2026'}\n"
            "thread: is_reply=false, quoted_messages=0"
        ),
        metadata=(
            "from: purchasing@new-company.com\n"
            "to: OUR COMPANY (UAE) <supply@our-company.com>\n"
            "subject: [Updated]VSL: NORTH STAR, QUOTATION: 0015-AB000001C, "
            "VENDOR: OUR COMPANY (UAE)\n"
            "mailbox: supply@our-company.com\n"
            "attachments: E_QUOT_0015-AB000001C_S001-01.pdf; "
            "E_QUOT_XLS_0015-AB000001C_S001-01.XLSX"
        ),
        latest=(
            "Dear Sir/Madam\n"
            'you may find attached our RFQ for Department "Engine Materials".\n'
            "m/t North Star - eta Fujairah subject to Hormuz straight opening\n"
            "Please consider quotation due date is 02/June/2026."
        ),
        answer=LLMClassification(
            category=EmailCategory.UPDATED_RFQ,
            direction=Direction.INBOUND_CUSTOMER,
            is_rfq=True,
            confidence=0.9,
            reasoning=(
                "The body is the customer's standard RFQ covering note again, but the subject "
                "carries an [Updated] marker and the same quotation reference, so this is a "
                "resend of an existing RFQ rather than a new one."
            ),
            evidence=[
                "subject prefix '[Updated]'",
                "same quotation reference 0015-AB000001C",
                "same RFQ form attachments",
            ],
        ),
    ),
    # 3. Our own outgoing quotation. No header block; the signature decides.
    FewShot(
        source_id="003",
        signals=(
            "sender_class: UNKNOWN\n"
            "template_marker_hits: ['own_outbound_quote']\n"
            "thread: is_reply=false, quoted_messages=0"
        ),
        metadata="from: -\nto: -\nsubject: -\nmailbox: supply@our-company.com\nattachments: -",
        latest=(
            "Good day Sir/Madam,\n"
            "Many thanks for your RFQ. We are pleased to submit our best offer for your kind "
            "perusal. Please see attachment for details.\n"
            "Please note:\n"
            "- Payment terms: As usual\n"
            "- Quote Validity 3 Working days\n"
            "We trust that you will find our prices competitive and look forward to receiving "
            "your valued order confirmation.\n"
            "Kind regards\n"
            "David Clark\n"
            "Customer Service Executive\n"
            "Our Company UAE\n"
            "david.clark@our-company.com"
        ),
        answer=LLMClassification(
            category=EmailCategory.OUTBOUND_OWN,
            direction=Direction.OUTBOUND_OWN,
            is_rfq=False,
            confidence=0.94,
            reasoning=(
                "The header is missing, but the author signs as a chandler Customer Service "
                "Executive and submits the chandler's own offer. RFQ vocabulary appears only because "
                "the message answers a customer's RFQ; the chandler is not being asked to quote."
            ),
            evidence=[
                "'We are pleased to submit our best offer'",
                "signature: David Clark, Our Company UAE",
                "sender address david.clark@our-company.com in the signature",
            ],
        ),
    ),
    # 4. One new sentence on top of a long quoted quotation. The history is not the email.
    FewShot(
        source_id="004",
        signals=(
            "sender_class: KNOWN_CUSTOMER\n"
            "subject_prefixes: ['RE:', 'FW:', 'FW:', '[UPDATED]']\n"
            "thread: is_reply=true, quoted_messages=1"
        ),
        metadata=(
            "from: purchasing@new-company.com\n"
            "to: OUR COMPANY (UAE) <supply@our-company.com>\n"
            "cc: michael.reed@our-company.com; David.Clark@our-company.com\n"
            "subject: RE: FW: Fw: [Updated]VSL: NORTH STAR, QUOTATION: 0015-AB000001C\n"
            "mailbox: supply@our-company.com\n"
            "attachments: -"
        ),
        latest=(
            "Dear David\n"
            "we need 100% Isopropanol , can you please recheck?\n"
            "Kind regards,\n"
            "James Miller\n"
            "Purchasing Officer\n"
            "NEW COMPANY LTD"
        ),
        quoted=(
            "--- quoted message 1 (from: supply@our-company.com, sent: Jun 16 2026) ---\n"
            "Many thanks for your RFQ. We are pleased to submit our best offer for your kind "
            "perusal. Item 12: Isopropyl Alcohol 99.9%, 25 LTR can ..."
        ),
        answer=LLMClassification(
            category=EmailCategory.CUSTOMER_CLARIFICATION,
            direction=Direction.INBOUND_CUSTOMER,
            is_rfq=False,
            confidence=0.92,
            reasoning=(
                "The newest message is a single question from the customer about the purity of "
                "an item the chandler has already quoted. The RFQ and the quotation both sit in the "
                "quoted history, so no new demand is being raised."
            ),
            evidence=[
                "'we need 100% Isopropanol , can you please recheck?'",
                "reply prefixes RE: FW: on an existing quotation subject",
                "the chandler's own quotation appears in the quoted history, not in the newest message",
            ],
        ),
    ),
    # 5. A vendor quoting to us. Prices and item codes, but inbound-supplier.
    FewShot(
        source_id="005",
        signals=(
            "sender_class: KNOWN_SUPPLIER\n"
            "thread: is_reply=false, quoted_messages=0"
        ),
        metadata=(
            "from: sales@technical-company.com\n"
            "to: John Baker <john.baker@our-company.com>\n"
            "subject: RE: Enquiry - Iso Propyl Alcohol\n"
            "mailbox: supply@our-company.com\n"
            "attachments: -"
        ),
        latest=(
            "Dear John,\n"
            "Thanks for your enquiry.\n"
            "We are pleased to submit our quotation as follows :-\n"
            "Iso Propyl Alcohol 99.9% - 25 LTR Can. Price : AED 100/= Can\n"
            "Prices quoted are Non-Inclusive of 5% Vat.\n"
            "Available ex-stock, subject to reconfirmation.\n"
            "Best Regards\n"
            "Robert Wilson, Managing Director, Sharjah - U.A.E"
        ),
        answer=LLMClassification(
            category=EmailCategory.SUPPLIER_CORRESPONDENCE,
            direction=Direction.INBOUND_SUPPLIER,
            is_rfq=False,
            confidence=0.93,
            reasoning=(
                "A UAE chemicals trading company answers an enquiry from a named the chandler buyer with "
                "its own prices. the chandler is the buyer here, so this is a vendor offer, not a "
                "customer asking the chandler to quote."
            ),
            evidence=[
                "'Thanks for your enquiry'",
                "'We are pleased to submit our quotation'",
                "addressed to a named the chandler buyer, John Baker",
                "quotes a price in AED to the chandler",
            ],
        ),
    ),
    # 6. A portal announcing a customer's new RFQ.
    FewShot(
        source_id="006",
        signals=(
            "sender_class: EXTERNAL_UNKNOWN\n"
            "portal: pal_econnect\n"
            "regex_hits: {'vessel_name': 'Blue Horizon', 'rfq_reference': "
            "'BH/O-0001/RFQ26', 'delivery_port': 'Fujairah'}\n"
            "thread: is_reply=false, quoted_messages=0"
        ),
        metadata=(
            "from: noreply@econnect.mariapps.com\n"
            "to: supply@our-company.com\n"
            "subject: RFQ Received - BH/O-0001/RFQ26, Old Company Shipping Limited, "
            "Blue Horizon\n"
            "mailbox: supply@our-company.com\n"
            "attachments: -"
        ),
        latest=(
            "*This is an auto-generated email. Please do not reply to this notification.\n"
            "You are requested to send Quotation for the below RFQ. Click "
            "https://econnect.mariapps.com/ to login the PAL-eConnect portal.\n"
            "Vessel Name: Blue Horizon\n"
            "RFQ No: BH/O-0001/RFQ26\n"
            "RFQ Title: Cabin (Refrigerator)\n"
            "Requisition Category: Stores\n"
            "Delivery Port: Fujairah"
        ),
        answer=LLMClassification(
            category=EmailCategory.PORTAL_RFQ_NOTIFICATION,
            direction=Direction.INBOUND_PORTAL,
            is_rfq=True,
            confidence=0.96,
            reasoning=(
                "An auto-generated PAL-eConnect notification tells the chandler that a customer has "
                "raised a new RFQ, giving the RFQ number, vessel and delivery port and asking "
                "the chandler to submit a quotation through the portal."
            ),
            evidence=[
                "'This is an auto-generated email'",
                "'You are requested to send Quotation for the below RFQ'",
                "RFQ No: BH/O-0001/RFQ26",
                "PAL-eConnect login link",
            ],
        ),
    ),
    # 7. The other portal case: a report about our own submitted quote.
    FewShot(
        source_id="011",
        signals=(
            "sender_class: PORTAL\n"
            "portal: shipserv\n"
            "regex_hits: {'quotation_reference': '1.11.00001.0.0'}\n"
            "thread: is_reply=false, quoted_messages=0"
        ),
        metadata=(
            "from: notifications@shipserv.com\n"
            "to: supply@our-company.com\n"
            "subject: Submitted Quote: 1.11.00001.0.0 - MSC Shipmanagement Ltd. for vessel "
            "Sea Voyager\n"
            "mailbox: supply@our-company.com\n"
            "attachments: -"
        ),
        latest=(
            "Submitted Quote: 1.11.00001.0.0 - Some Company Shipmanagement Ltd. for vessel "
            "Sea Voyager\n"
            "ELECTRICAL TOOLS & MATERIALS FOR CCTV\n"
            "Supplier: Our Company Shipchandlers (TN-00001)\n"
            "QOT Ref: 1.11.00001.0.0\n"
            "RFQ Ref: 10000001\n"
            "Valid until: 2/27/26, 9:24 AM (GMT)\n"
            "Best Regards, The ShipServ Team."
        ),
        answer=LLMClassification(
            category=EmailCategory.QUOTE_STATUS_NOTIFICATION,
            direction=Direction.INBOUND_PORTAL,
            is_rfq=False,
            confidence=0.94,
            reasoning=(
                "ShipServ is reporting the status of a quotation the chandler itself submitted, naming "
                "the chandler as the supplier and giving its QOT reference. No new customer demand is "
                "being raised."
            ),
            evidence=[
                "'Submitted Quote: 1.11.00001.0.0'",
                "'Supplier: Our Company Shipchandlers (TN-00001)'",
                "QOT Ref present, no request to quote",
            ],
        ),
    ),
    # 8. Colleagues talking to each other. Again no header - the signature decides.
    FewShot(
        source_id="009",
        signals=(
            "sender_class: UNKNOWN\n"
            "recipients_are_internal_only: false\n"
            "thread: is_reply=true, quoted_messages=2"
        ),
        metadata="from: -\nto: -\nsubject: -\nmailbox: supply@our-company.com\nattachments: -",
        latest=(
            "Dear Michael,\n"
            "Prices has been submitted in the CUSTOMER PORTAL.\n"
            "Regards,\n"
            "Robert Hall\n"
            "Customer Service Executive\n"
            "Our Company UAE\n"
            "robert.hall@our-company.com"
        ),
        quoted=(
            "--- quoted message 1 (from: michael.reed@our-company.com, sent: 27 February 2026) "
            "---\nKINDLY UPLOAD WITH DATASHEET\n"
            "Subject: FW: RFQ Received - BH/O-0001/RFQ26, Old Company Shipping Limited"
        ),
        answer=LLMClassification(
            category=EmailCategory.INTERNAL,
            direction=Direction.INTERNAL,
            is_rfq=False,
            confidence=0.93,
            reasoning=(
                "A chandler customer service executive reports to a chandler colleague that prices have "
                "been submitted. Both parties are chandler staff, so there is nothing for the "
                "Mailbox Team to route."
            ),
            evidence=[
                "'Prices has been submitted in the CUSTOMER PORTAL'",
                "signature Robert Hall, Our Company UAE, robert.hall@our-company.com",
                "addressed to Eric, a chandler colleague",
            ],
        ),
    ),
    # 9. Urgent chasing of an existing quote. Urgency is not a new RFQ.
    FewShot(
        source_id="008",
        signals=(
            "sender_class: KNOWN_CUSTOMER\n"
            "regex_hits: {'vessel_name': 'BLUE HORIZON', 'rfq_reference': "
            "'BH/O-0001/RFQ26', 'urgency_markers': ['U R G E N T']}\n"
            "thread: is_reply=false, quoted_messages=0"
        ),
        metadata=(
            "from: procurement@old-company.com\n"
            "to: supply@our-company.com\n"
            "subject: M/T BLUE HORIZON AT FUJAIRAH - URGENT\n"
            "mailbox: supply@our-company.com\n"
            "attachments: -"
        ),
        latest=(
            "FM : OLD COMPANY SHIPPING LIMITED\n"
            "TO : OUR COMPANY SHIPCHANDLERS LLC (UAE)\n"
            "M/T BLUE HORIZON AT FUJAIRAH\n"
            "***  U R G E N T  ***\n"
            "Ref. your quotation no. 1.11.2026.00001 received on 27-2-26 for our inquiry "
            "BH/O-0001/RFQ26, please advise if offered refrigerator is still available and if "
            "your quotation is still valid.\n"
            "Your urgent reply would be much appreciated.\n"
            "Michael Brown, Procurement Officer, Old Company Shipping"
        ),
        answer=LLMClassification(
            category=EmailCategory.CUSTOMER_CLARIFICATION,
            direction=Direction.INBOUND_CUSTOMER,
            is_rfq=False,
            confidence=0.9,
            priority_hint=Priority.URGENT,
            reasoning=(
                "The customer is chasing a quotation the chandler already sent, asking whether the item "
                "is still available and the price still valid. It cites the chandler's quotation number, "
                "so it comments on an existing RFQ rather than raising a new one."
            ),
            evidence=[
                "'Ref. your quotation no. 1.11.2026.00001'",
                "'please advise if ... your quotation is still valid'",
                "'*** U R G E N T ***'",
                "references the existing inquiry BH/O-0001/RFQ26",
            ],
        ),
    ),
    # 10. The hardest boundary: detailed new specifications inside an existing RFQ.
    FewShot(
        source_id="010",
        signals=(
            "sender_class: KNOWN_CUSTOMER\n"
            "regex_hits: {'quotation_reference': '1.11.00001.0.0'}\n"
            "thread: is_reply=true, quoted_messages=1"
        ),
        metadata=(
            "from: john.davis@some-company.com\n"
            "to: Our Company Ship Supply - UAE <supply@our-company.com>\n"
            'subject: FW: VARIANCE IN: Quote 1.11.00001.0.0 for RFQ \'10000001\' vessel '
            "'Sea Voyager'\n"
            "mailbox: supply@our-company.com\n"
            "attachments: -"
        ),
        latest=(
            "Dear Sirs,\n"
            "Please find below reply from vessel:\n"
            "QUOTE:\n"
            "Please be informed that from the mentioned req. only Item N*3 is suitable. "
            "The Items N*1, 2 not suitable (RL 378-03994).\n"
            "Requested an Electrical Rotary Hammer (Rotary Drill) and SDS-Plus drill bits for "
            "CCTV cable installation.\n"
            "For Item 1: Rotary Hammer Drill - 1 pc, 220 VAC, Impact Energy 2.4-2.8 Joules\n"
            "For Item 2: SDS-Plus Drill Bits 8mm x 300mm - 2 pcs, 10mm x 500mm - 2 pcs\n"
            "UNQUOTE"
        ),
        quoted=(
            "--- quoted message 1 (from: supply@our-company.com, sent: Jun 2026) ---\n"
            "VARIANCE IN: Quote 1.11.00001.0.0 from Our Company Shipchandlers for RFQ '10000001'"
        ),
        answer=LLMClassification(
            category=EmailCategory.CUSTOMER_CLARIFICATION,
            direction=Direction.INBOUND_CUSTOMER,
            is_rfq=False,
            confidence=0.72,
            reasoning=(
                "The vessel rejects two quoted items and gives detailed specifications for "
                "replacements. This is a correction inside a quotation the chandler has already priced, "
                "not a fresh RFQ, though the amount of new specification makes UPDATED_RFQ a "
                "realistic competing reading."
            ),
            evidence=[
                "'Please find below reply from vessel'",
                "'The Items N*1, 2 not suitable'",
                "replies into an existing quote thread, 1.11.00001.0.0",
                "no quotation due date and no RFQ form attachment",
            ],
        ),
    ),
]
