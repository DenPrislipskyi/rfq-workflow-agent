"""What the model is allowed to answer with.

Two questions in this stage, and one of them is asked once per file.

`FileRead` covers a whole file in one answer - what it is, and either what its
columns mean or what its items say. It used to be two calls to the same file,
one asking "what is this" and one asking "now name the columns", both showing
the model the same sample. That was one call too many.

`ItemsFromText` is the continuation: a scan that needs several parts keeps
transcribing with it once the first answer has settled what the file is.
"""

from pydantic import BaseModel, Field

from src.services.extraction.models import HeaderField, ItemField


class ColumnAssignment(BaseModel):
    """One column of the customer's table, and what it supplies."""

    # The letter shown in the prompt: "B", "AA".
    column: str
    field: ItemField


class TableMapping(BaseModel):
    """Which columns of one table fill which fields of the template.

    Assignments are a list rather than a dict: structured-output modes handle a
    fixed shape far better than free-form object keys, and the model has to name
    the column explicitly instead of relying on position.
    """

    # Which table this is, as numbered in the prompt. A sheet can hold several -
    # "DECK STORES" above "ENGINE STORES" - and each has its own header row.
    table: int = 1
    # The row the headings are on. Code guesses it first; this is the model's
    # chance to correct the one thing code is most likely to get wrong.
    header_row: int
    assignments: list[ColumnAssignment] = Field(default_factory=list)

    def column_for(self, field: ItemField) -> str | None:
        return next((one.column for one in self.assignments if one.field is field), None)

    @property
    def mapped_columns(self) -> set[str]:
        return {one.column for one in self.assignments}


class ReadItem(BaseModel):
    """One line item transcribed out of prose, a scan or a photo.

    Only `description` is required. Half the RFQs in the sample carry no item
    code at all, and a schema that demanded one would invite the model to make
    one up.
    """

    description: str
    customer_item_code: str | None = None
    quantity: str | None = None
    uom: str | None = None
    # Which page or image it was read from, using one of the labels the prompt
    # listed. A wrong label falls back to the part, so a bad citation costs
    # precision rather than the item.
    page: str | None = None


class FileRead(BaseModel):
    """One attachment, read in a single answer.

    `tables` and `items` are alternatives, not both: a file whose items sit in
    a grid is copied by code once the columns are named, and one whose items
    are in prose or on a photograph has to be transcribed. The prompt shows
    whichever the file turned out to have.
    """

    what: str
    # True only for the list of goods we are being asked to quote. A drawing
    # carrying part numbers is not one, and the difference decides whether this
    # file's contents become line items or context.
    has_item_list: bool
    # Product identifiers copied verbatim: manufacturer, model, serial number,
    # IMPA code, rating. Empty when the file shows none.
    facts: list[str] = Field(default_factory=list)
    # One per table the prompt showed, when the items are in those tables.
    tables: list[TableMapping] = Field(default_factory=list)
    # The items themselves, when there was no table to name.
    items: list[ReadItem] = Field(default_factory=list)


class ItemsFromText(BaseModel):
    """Every item in one further part of a document already recognised."""

    items: list[ReadItem] = Field(default_factory=list)


class FoundValue(BaseModel):
    """Something the model read, and where it read it."""

    # Exactly as written in the source. "12 Oct" stays "12 Oct".
    value: str
    # One of the labels the prompt listed: "email.body", "Requisition.xlsx".
    source: str


class ExtractedField(FoundValue):
    """One header field the model claims to have found."""

    field: HeaderField


class MissingField(BaseModel):
    """A starred field the model looked for and did not find, and where it looked.

    The other half of `fields`, and the only half a model can supply: code
    knows a cell is empty, but only the reader that saw the whole email knows
    whether the customer never wrote the value, wrote it somewhere unusable, or
    wrote something that is not what the cell wants.
    """

    field: HeaderField
    # One sentence, in the model's own words, about where it looked. Never a
    # guess at the value and never advice - the desk decides what to do next.
    why: str


class HeaderExtraction(BaseModel):
    """The header fields present in this RFQ.

    A list of what was found, not a form with fifteen slots to fill. The
    difference is the whole guardrail: a slot invites a value, while an absent
    entry costs the model nothing and is the honest answer for a field the
    customer never wrote. There is no way to say "N/A" here, and no null to
    misread later.
    """

    fields: list[ExtractedField] = Field(default_factory=list)
    # Which company sent this RFQ, as the email spells it. Separate from the
    # fields above because it is not a cell on the form: it is what the sender
    # code is looked up from, and the lookup needs the name the customer uses
    # for itself, not a code.
    customer_company: FoundValue | None = None
    # The starred fields that are not in `fields`, each with one sentence about
    # why. Empty when everything starred was found - there is nothing to explain
    # about a cell that is filled.
    not_found: list[MissingField] = Field(default_factory=list)
