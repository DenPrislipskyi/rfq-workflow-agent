"""What the extraction stage knows about one attachment.

These live beside the service rather than in `domain`: a file's role is a step
in this pipeline, not something the business would recognise. The domain keeps
what an operator would name - categories, regions, actions.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from src.infrastructure.documents.models import Document

# Warning codes this stage can raise. Same reason they are collected in one
# place as in `documents.models`: the gate downstream maps every code to "can
# this still go through without a person?".
READ_FAILED = "file_could_not_be_read"
NOTHING_TO_READ = "nothing_to_read"
NO_TABLE_FOUND = "no_table_found"
MAPPING_UNVERIFIED = "column_mapping_unverified"
UNMAPPED_COLUMNS = "unmapped_columns"
SEVERAL_TABLES = "several_tables_in_one_sheet"
NO_ITEM_ROWS = "no_item_rows"
HEADER_FAILED = "header_extraction_failed"
FIELD_WITHOUT_SOURCE = "field_without_source"
FIELD_FROM_UNKNOWN_SOURCE = "field_from_unknown_source"
FIELD_CLAIMED_TWICE = "field_claimed_twice"
FIELD_IS_NOT_A_MODELS_TO_FILL = "field_answered_that_is_looked_up_not_read"
PLACEHOLDER_VALUE = "placeholder_value"
EXPLAINED_A_FIELD_IT_FOUND = "explained_a_field_it_also_filled"
TRANSCRIPTION_FAILED = "transcription_failed"
TRANSCRIPTION_PARTIAL = "transcription_partial"
DUPLICATE_ITEM_SOURCE = "same_items_attached_twice"
# Two files, the same article, different quantities - a revision attached
# beside the list it replaces. Never resolved by code: only the covering
# sentence says which one stands.
SAME_ITEM_TWO_QUANTITIES = "same_item_two_quantities_in_two_files"
NO_ITEMS_ANYWHERE = "no_items_found_in_this_rfq"


class DocumentRole(StrEnum):
    """What this file is for, and therefore which reader gets it next.

    The role is not a property of the file type - a PDF can be any of these -
    so it is decided per file from what the model saw plus what stage A
    recovered. The member names the route.
    """

    # The requested items are in a table. Code reads the cells; the model is
    # asked only what the columns mean.
    ITEM_GRID = "ITEM_GRID"
    # The requested items are in prose, or on a scan. The model has to read them.
    ITEM_TEXT = "ITEM_TEXT"
    # Facts about products, but no list of what to quote: a drawing, a photo of
    # a nameplate, a specification sheet.
    SUPPORTING = "SUPPORTING"
    # Stage A recovered nothing from it. Never sent to a model.
    EMPTY = "EMPTY"
    # The model could not be asked. Not the same as "nothing in it", and the
    # difference matters: this one still needs a person.
    UNREAD = "UNREAD"


@dataclass(frozen=True, slots=True)
class ReadDocument:
    """One attachment, after it has been read.

    One type where there used to be two. The model is asked about a file once -
    what it is, and either what its columns mean or what its items say - so
    there is one answer per file and one place to keep it.
    """

    document: Document
    role: DocumentRole
    # One sentence, in the model's words. Goes into the header prompt as context
    # and into the forwarded email when something needs explaining to a person.
    what: str = ""
    # Product identifiers copied out verbatim: manufacturer, model, serial, IMPA.
    facts: list[str] = field(default_factory=list)
    # Numbered within this file; the pipeline renumbers across the whole RFQ.
    items: list["LineItem"] = field(default_factory=list)
    # Columns that held data and went nowhere, e.g. "F (REMARKS)".
    unmapped_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return self.document.origin

    @property
    def holds_items(self) -> bool:
        return self.role in (DocumentRole.ITEM_GRID, DocumentRole.ITEM_TEXT)

    @property
    def needs_a_person(self) -> bool:
        return self.role is DocumentRole.UNREAD


class ItemField(StrEnum):
    """A column of the template's item grid that a customer table can supply.

    Only five. `ITEM CODE`, the prices and the supplier are the template's
    columns C and G to J, and this task must leave them blank - they belong to
    a later matching step, and filling them here would be inventing.

    `SR_NO` is here even though the template numbers its own rows: naming the
    customer's numbering column stops it being mistaken for an item code.
    """

    SR_NO = "sr_no"
    CUSTOMER_ITEM_CODE = "customer_item_code"
    DESCRIPTION = "description"
    QUANTITY = "quantity"
    UOM = "uom"


@dataclass(frozen=True, slots=True)
class LineItem:
    """One row the customer asked us to quote.

    Values stay exactly as the customer wrote them - "2 coil" is not split, "1,5"
    is not converted. Normalization knows the field it is filling; a reader does
    not, and a reader that guesses loses the original.
    """

    sr_no: int
    description: str | None = None
    customer_item_code: str | None = None
    quantity: str | None = None
    uom: str | None = None
    # "Requisition.xlsx#Sheet1 row 27" - the cell this row came out of.
    source: str = ""


class HeaderField(StrEnum):
    """A header cell of the template that gets filled from the RFQ.

    Sixteen, not twenty. The rest of the form is not read out of the email at
    all: the branch comes from the region rules that already route it, the
    subject follows from the triage category, and `A1` is a formula.

    Fifteen of these a model fills. `SENDER_CODE` is the exception - see
    `DERIVED_FIELDS`.
    """

    VESSEL_NAME = "vessel_name"          # C2
    IMO = "imo"                          # C3
    RFQ_REFERENCE = "rfq_reference"      # C4
    CUSTOMER_CONTACT = "customer_contact"      # C6
    CUSTOMER_PHONE = "customer_phone"          # C7
    CUSTOMER_EMAIL = "customer_email"          # C8
    PERSON_DESIGNATION = "person_designation"  # C9
    DELIVERY_PORT = "delivery_port"      # H2
    ETA = "eta"                          # H3
    ETD = "etd"                          # H4
    QUOTE_BEFORE = "quote_before"        # H5
    REQUESTED_DELIVERY = "requested_delivery"  # H6
    DELIVERY_ADDRESS = "delivery_address"      # H7
    CURRENCY = "currency"                # H8
    RFQ_TYPE = "rfq_type"                # H11
    # Not a model's to answer: it is the customer's code in the master
    # workbook, and the mapping document forbids inventing one. Code looks it
    # up from the company that sent the email.
    SENDER_CODE = "sender_code"          # H9


# Filled by looking something up rather than by reading it. The model is never
# shown these, and an answer naming one is refused: a plausible-looking code
# that belongs to another customer is worse than an empty cell.
DERIVED_FIELDS = frozenset({HeaderField.SENDER_CODE})

# What the model is asked for, in the order the form lists it.
MODEL_FILLED_FIELDS = [name for name in HeaderField if name not in DERIVED_FIELDS]


# Marked with * in the template. A copy missing one of these is not finished,
# which is what decides whether the forward carries a caveat.
REQUIRED_HEADER_FIELDS = frozenset(
    {
        HeaderField.VESSEL_NAME,
        HeaderField.IMO,
        HeaderField.RFQ_REFERENCE,
        HeaderField.DELIVERY_PORT,
        HeaderField.CURRENCY,
        HeaderField.RFQ_TYPE,
    }
)

# Starred on the form as well, and left out of the set above on purpose: the
# sender code exists nowhere but the master workbook, so while the matching is
# switched off there is no email it could ever be read from, and reporting it
# missing on every RFQ says nothing. Goes back in with `MASTER_MATCHING_ENABLED`.
REQUIRED_WITH_MASTER = REQUIRED_HEADER_FIELDS | {HeaderField.SENDER_CODE}


@dataclass(frozen=True, slots=True)
class HeaderValue:
    """One field, and the place it was read from.

    `source` is not decoration. A value that cannot name where it came from was
    invented, and the task says not to invent - so the reader drops it rather
    than passing it on.
    """

    value: str
    source: str


@dataclass(frozen=True, slots=True)
class RefusedField:
    """A field the model answered and the reader would not take.

    Kept rather than only logged. A refused field leaves the same empty cell as
    a field nobody wrote, and the two are the opposite problem: one says the
    email had nothing, the other says the model answered `N/A` or cited
    nothing. Without this, the journal has the reason code and no idea which
    field or value it was about.
    """

    field: HeaderField
    value: str
    why: str


@dataclass(frozen=True, slots=True)
class RfqHeader:
    """The top of the template, as the customer wrote it.

    Values are verbatim: "Jebel Ali", not `UAE - JEBEL ALI`; "12 Oct", not a
    date. Mapping them onto what the workbook accepts is B6's job, and it needs
    the original to do it.
    """

    fields: dict[HeaderField, HeaderValue] = field(default_factory=dict)
    # The company that sent the RFQ, as the email spells it. Not a cell on the
    # form - `A1` derives the customer's name from the sender code - but it is
    # what the sender code is looked up from, so it is read and kept like any
    # other value, with its source.
    customer_company: HeaderValue | None = None
    # What the model answered and the reader would not take, with the reason.
    refused: list[RefusedField] = field(default_factory=list)
    # Starred field -> one sentence from the model about where it looked and
    # did not find it. The only account of an absence that anything can give:
    # code sees an empty cell and cannot tell "nobody wrote it" from "it is
    # there and the reader missed it".
    not_found: dict[HeaderField, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def value(self, name: HeaderField) -> str | None:
        found = self.fields.get(name)
        return found.value if found else None

    def source(self, name: HeaderField) -> str | None:
        found = self.fields.get(name)
        return found.source if found else None

    @property
    def missing_required(self) -> list[HeaderField]:
        """Starred fields the model was asked for and did not find.

        The sender code is starred too, but it is not on this list: nothing in
        an email contains it, so reporting it here would flag every RFQ ever
        read. `RfqExtraction.missing_required` covers it, after the lookup that
        actually fills it has run.
        """
        return [
            name
            for name in MODEL_FILLED_FIELDS
            if name in REQUIRED_HEADER_FIELDS and name not in self.fields
        ]

    @property
    def is_complete(self) -> bool:
        return not self.missing_required


# B6 raises these when a verbatim value cannot be mapped onto what the workbook
# accepts. Every one of them means a blank cell, never a guessed one.
PORT_NOT_IN_LIST = "port_not_in_branch_list"
PORT_AMBIGUOUS = "port_matched_several"
CURRENCY_NOT_ACCEPTED = "currency_not_accepted"
IMO_INVALID = "imo_check_digit_failed"
DATE_AMBIGUOUS = "date_could_be_read_two_ways"
DATE_UNREADABLE = "date_not_understood"
# Read without trouble and still not a day: `31/02`, `40/07`, `29 Feb` in a
# year that has no 29th. Its own code because it is the customer's typo rather
# than our parser's limit, and those send a person to two different places.
DATE_IMPOSSIBLE = "date_is_not_a_real_day"
RFQ_TYPE_FELL_BACK = "rfq_type_fell_back_to_others"
CLIENT_NOT_IN_LIST = "customer_not_in_the_master_client_list"
CLIENT_MATCHED_SEVERAL = "customer_matched_several_clients"
CLIENT_NOT_NAMED = "customer_company_not_named_in_the_email"
# The branch's own column had nothing for this client, so the code came from
# `Lookup`. Worth saying: the two disagree in the one finished RFQ we can check.
# The mapping document asks for a best candidate rather than a blank when
# several entries look alike, so these two mean "filled in, but check it" -
# not "left empty". They are the only warnings that ride on a written value.
PORT_WAS_A_GUESS = "port_was_the_closest_of_several"
CLIENT_WAS_A_GUESS = "customer_was_the_closest_of_several"
# The master had no entry for it, so the customer's own wording went in. Not a
# refusal: the master is a dictionary for improving a value, not a gate that
# decides whether it may be written at all.
PORT_AS_WRITTEN = "port_written_as_the_customer_spelled_it"
CURRENCY_AS_WRITTEN = "currency_written_as_the_customer_spelled_it"
IMO_AS_WRITTEN = "imo_written_as_given_the_check_digit_failed"
SENDER_CODE_NOT_IN_MASTER = "customer_has_no_sender_code_in_the_master"


@dataclass(frozen=True, slots=True)
class NormalizedHeader:
    """Header values as the workbook will take them.

    Two dictionaries rather than one of unions: the template's date cells carry
    number formats and have to be written as serials, while everything else is
    text. Stage C already knows which cell is which, so keeping them apart
    saves it from having to ask.
    """

    text: dict[HeaderField, str] = field(default_factory=dict)
    dates: dict[HeaderField, date] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Field -> why it did not survive. This is what a person is shown when the
    # forward carries a caveat.
    dropped: dict[HeaderField, str] = field(default_factory=dict)
    # Field -> why it deserves a second look. Filled in, unlike `dropped`: the
    # mapping document asks for a best candidate plus a flag where several
    # entries look alike, and a value nobody is told to check is a value nobody
    # checks.
    checked: dict[HeaderField, str] = field(default_factory=dict)

    def has(self, name: HeaderField) -> bool:
        return name in self.text or name in self.dates
