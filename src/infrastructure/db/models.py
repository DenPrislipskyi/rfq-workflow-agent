"""The record, as tables.

This is `Database/<email>/email.json` taken apart. One folder per email becomes
one row in `emails` plus the rows that hang off it, and the files that sat
beside the JSON go to blob storage - the row keeps the key, not the bytes.

    emails                 one email, and what a list has to show about it
    email_verdicts         what the agent decided it is                 1:1
    email_extractions      what was read out of the RFQ                 1:1
    email_deliveries       where it went, or why it did not             1:1
    email_files            attachments and the filled form              1:N
    rfq_lines              one line of the RFQ, matched or refused      1:N
    rfq_line_candidates    what a refused line was offered              1:N

Three decisions run through all of it.

**Closed sets are stored as text, not as Postgres enums.** `category`,
`recommended_action`, `how` and the rest are `StrEnum` in `src/domain/enums.py`
and arrive here as their values. A Postgres enum would turn every new category
into a migration, and the categories are still being discovered. The domain
enum is the constraint that matters; the column records what it said.

**What the form spells out gets a column; the rest of the header stays whole.**
Vessel, IMO and port are what the list and the RFQ page show, so they are
queryable. Everything else the header reader returns lives in `header` as JSON,
because that shape grows with the form and nothing here should have to notice.

**The sheet's row is kept entire.** A matched product carries all sixteen of its
columns, a candidate the same. Picking the useful-looking ones today is how you
lose the column somebody needs in March.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    Numeric,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class FileRole(StrEnum):
    """Which side of the exchange a file is.

    A real enum, unlike the verdict's vocabulary: there are two kinds of file
    and there will not be a third without somebody deciding to add one.
    """

    ATTACHMENT = "attachment"
    FORM = "form"


class Email(Base):
    """One email the agent has seen, whatever it turned out to be.

    Not "one RFQ": a supplier's quotation and a marketing blast get a row too.
    What made the list a list of RFQs was `verdict.is_rfq`, and it still is.
    """

    __tablename__ = "emails"

    # The record's own id - `2026-09-13T17-03-03Z__b7d5d04d`. Kept as the
    # primary key rather than swapped for a serial, because it is already in
    # every URL the front end holds and in every folder name on disk.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # The desk's own number for this RFQ - `RFQ-0042`. Null for an email
    # that is not one, which is one in five of them.
    #
    # Not the primary key, and deliberately: `id` addresses a record the moment
    # it is opened, whatever the email turns out to be, while this is issued
    # only once the verdict says RFQ. Unique, because it goes out in a letter
    # and two RFQs carrying it would be two RFQs nobody can tell apart - which
    # is also why the numbering runs on rather than restarting each January.
    #
    # The number comes from the `rfq_reference_seq` sequence, which the
    # migration makes: a counter is exactly what a sequence is, and asking
    # Postgres for the next one cannot hand the same number out twice.
    rfq_reference: Mapped[str | None] = mapped_column(String(32), unique=True)

    # Ties this row to `data/decisions.jsonl` and to the filled workbook, both
    # of which are named after the decision.
    decision_id: Mapped[str | None] = mapped_column(String(64))
    # Graph's own id. Unique where present: the same message arriving twice
    # through a re-delivered webhook must not become two rows.
    message_id: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16), default="outlook")

    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    mailbox: Mapped[str | None] = mapped_column(String(320))
    sender_name: Mapped[str | None] = mapped_column(String(256))
    sender_address: Mapped[str | None] = mapped_column(String(320))
    # Shown and never filtered on, so they stay as they came. Named
    # `recipients` rather than `to`, which is a reserved word: SQLAlchemy
    # quotes it, and every hand-written query after that has to remember to.
    recipients: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    cc: Mapped[list[dict]] = mapped_column(JSONB, default=list)

    subject: Mapped[str | None] = mapped_column(Text)
    body_chars: Mapped[int] = mapped_column(Integer, default=0)
    # Where the body text lives in blob storage. The text itself is not a
    # column: it is the one field that can be megabytes, and nothing queries it.
    body_key: Mapped[str | None] = mapped_column(String(1024))

    # The Outlook categories stamped on the message. An array rather than JSON
    # because "which emails carry SSG Not Sent" is a real question.
    labels: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    # Whether Outlook took them. False means the mailbox copy carries no mark,
    # which is also what stops this email being forwarded twice.
    labelled: Mapped[bool | None] = mapped_column(Boolean)

    # Set when the pipeline raised on this email. The row exists either way: an
    # email that broke the agent is the one somebody most needs to see.
    error: Mapped[str | None] = mapped_column(Text)

    verdict: Mapped["EmailVerdict | None"] = relationship(
        back_populates="email", cascade="all, delete-orphan", uselist=False
    )
    extraction: Mapped["EmailExtraction | None"] = relationship(
        back_populates="email", cascade="all, delete-orphan", uselist=False
    )
    delivery: Mapped["EmailDelivery | None"] = relationship(
        back_populates="email", cascade="all, delete-orphan", uselist=False
    )
    files: Mapped[list["EmailFile"]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="EmailFile.id",
    )
    lines: Mapped[list["RfqLine"]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="RfqLine.index",
    )
    inquiries: Mapped[list["RfqInquiry"]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="RfqInquiry.supplier",
    )
    approval: Mapped["RfqApproval | None"] = relationship(
        back_populates="email", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("message_id", name="emails_message_id_key"),
        # The list is "newest first", always, and it is the only query that runs
        # on every page load.
        Index("emails_received_at_idx", received_at.desc()),
    )


class EmailVerdict(Base):
    """What the agent decided this email is, and what it was going on.

    Its own table rather than columns on `emails` because it is one answer from
    one step: an email whose classification failed has a row and no verdict, and
    that is a different state from a verdict with low confidence.
    """

    __tablename__ = "email_verdicts"

    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True
    )

    category: Mapped[str] = mapped_column(String(64))
    # The whole list of RFQs is this column. Indexed for that reason alone.
    is_rfq: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_action: Mapped[bool] = mapped_column(Boolean, default=False)
    recommended_action: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))

    # The model's own opinion of itself, 0-1. Shown to a person; no threshold
    # in the service reads it, because it is not calibrated against anything.
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    # `LLM` or the name of the cheap rule that answered without one.
    decision_path: Mapped[str] = mapped_column(String(32))

    reasoning: Mapped[str] = mapped_column(Text, default="")
    # The quoted lines the verdict was taken from. Six months on, "why did this
    # look like an RFQ?" has to be answerable, and this is the answer.
    evidence: Mapped[list[str]] = mapped_column(JSONB, default=list)
    rule_hits: Mapped[list[str]] = mapped_column(JSONB, default=list)
    model: Mapped[str | None] = mapped_column(String(64))

    email: Mapped[Email] = relationship(back_populates="verdict")

    __table_args__ = (Index("email_verdicts_is_rfq_idx", "is_rfq"),)


class EmailExtraction(Base):
    """What was read out of the RFQ, in the shape a page can show."""

    __tablename__ = "email_extractions"

    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True
    )

    items: Mapped[int] = mapped_column(Integer, default=0)
    # Every starred field filled and at least one item.
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    missing_required: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # `scanned_pdf`, `same_items_attached_twice`, `column_mapping_unverified` -
    # the codes that say what the reader could not do.
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # The three the screens show. Everything else the header reader returned is
    # in `header` beside them, verbatim, with its source and raw value.
    vessel_name: Mapped[str | None] = mapped_column(String(256))
    imo: Mapped[str | None] = mapped_column(String(32))
    delivery_port: Mapped[str | None] = mapped_column(String(128))
    header: Mapped[dict] = mapped_column(JSONB, default=dict)

    email: Mapped[Email] = relationship(back_populates="extraction")


class EmailDelivery(Base):
    """Where the RFQ went, or why it did not go."""

    __tablename__ = "email_deliveries"

    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True
    )

    # `SENT`, `NO_REGION`, `FAILED`. A row with an outcome that is not SENT is
    # an RFQ waiting for a person, and the label beside it says so too.
    outcome: Mapped[str] = mapped_column(String(32))
    forwarded_to: Mapped[str | None] = mapped_column(String(320))
    cc: Mapped[list[str]] = mapped_column(JSONB, default=list)
    region: Mapped[str | None] = mapped_column(String(16))
    # Which rule chose the desk: `delivery_port`, `keyword`, `mailbox`. The
    # first question asked when an RFQ lands on the wrong one.
    region_rule: Mapped[str | None] = mapped_column(String(32))
    attached: Mapped[str | None] = mapped_column(String(512))

    email: Mapped[Email] = relationship(back_populates="delivery")


class EmailFile(Base):
    """One file: an attachment as it arrived, or the form we filled.

    Both in one table because they are the same thing to everything downstream -
    a name, a type, a size and somewhere to fetch the bytes. `role` is what
    tells them apart, and it is the only reason a second table was considered.
    """

    __tablename__ = "email_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )

    role: Mapped[FileRole] = mapped_column(Enum(FileRole, name="file_role_enum"))
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # The name this file has inside its record - the customer's, sanitised and
    # made unique. It is what the front end puts in a URL, so it survives the
    # move off disk unchanged: `/files/Requisition.xlsx` means the same thing
    # whether the bytes are in a folder or in a container.
    saved_as: Mapped[str | None] = mapped_column(String(256))
    # Where those bytes actually are. Null when they never reached us, and then
    # `note` says which of the reasons it was: a OneDrive link with no bytes to
    # keep, a download Graph refused, or an email the cheap rules answered
    # without opening. An empty cell and a missing file are different facts.
    blob_key: Mapped[str | None] = mapped_column(String(1024))
    note: Mapped[str | None] = mapped_column(Text)

    email: Mapped[Email] = relationship(back_populates="files")


class RfqLine(Base):
    """One line of an RFQ, and the product it was matched to - or was not.

    The left half is the customer's, unconverted: their code, their words, their
    quantity in their own unit. The right half is ours. They are kept apart here
    as they are on the screen, because the whole point is comparing them.
    """

    __tablename__ = "rfq_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )
    # The line's number within the RFQ, as the reader numbered it.
    index: Mapped[int] = mapped_column(Integer)

    # As the reader got it out of the file, and the text the sheet was actually
    # searched with. Both, because a match that turns out wrong is explained by
    # the difference - and the two are different sentences whenever a quoted
    # code was overruled: the search then runs on the sheet's own wording.
    verbatim: Mapped[str] = mapped_column(Text, default="")
    query: Mapped[str] = mapped_column(Text, default="")
    customer_code: Mapped[str | None] = mapped_column(String(64))
    # Text, not numbers: "1,5" and "2 coil" are what the customer wrote, and a
    # column typed as numeric would have to guess at both.
    quantity: Mapped[str | None] = mapped_column(String(64))
    uom: Mapped[str | None] = mapped_column(String(32))

    # Null for a refusal. The sheet's row goes in whole beside it.
    item_code: Mapped[str | None] = mapped_column(String(64))
    item_description: Mapped[str] = mapped_column(Text, default="")
    item: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[int | None] = mapped_column(Integer)
    # What a person settled on. Null until somebody does, and null again the
    # moment they change their mind: the screen calls the first state "Review
    # Needed" and the second one is the same state, not a third.
    confirmed_item_code: Mapped[str | None] = mapped_column(String(64))
    # What a supplier quoted for one unit of it. Null until one of them
    # answers. `Numeric` rather than `Float`: this is money, and money that
    # rounds differently on two machines is money somebody argues about.
    offer_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # When that price came in. Beside the price rather than on the supplier:
    # a supplier answers about lines, and a reply that covered two of three
    # would date the third one too.
    offer_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What this line sells for, per unit, as it stood at approval. Kept rather
    # than recomputed: cost and margin both move afterwards, and a quotation
    # that followed them would stop being the number the customer was given.
    approved_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # `code_confirmed`, `code_rejected`, `search`, `none`. The first thing an
    # operator looks at: "their code was wrong" and "we found it by its words"
    # are different things to be told.
    how: Mapped[str] = mapped_column(String(32), default="none")
    why: Mapped[str] = mapped_column(Text, default="")

    email: Mapped[Email] = relationship(back_populates="lines")
    candidates: Mapped[list["RfqLineCandidate"]] = relationship(
        back_populates="line",
        cascade="all, delete-orphan",
        order_by="RfqLineCandidate.rank",
    )

    __table_args__ = (
        # The reader numbers lines within one RFQ, so the pair is the identity.
        UniqueConstraint("email_id", "index", name="rfq_lines_email_id_index_key"),
    )


class RfqInquiry(Base):
    """One request for a quote, as it went out to one supplier.

    The text is kept whole. It is editable before it goes, and the sentence
    that was actually sent is the one worth keeping - not the one today's
    template would rebuild from the same lines.

    No email address: the sheet carries none, so the firm's name is both the
    address and the identity, and one letter goes to each.
    """

    __tablename__ = "rfq_inquiries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )
    supplier: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Which lines it asked about, by their index in the record. A list rather
    # than a join table: nothing is ever queried by it, and the letter is read
    # as a whole or not at all.
    lines: Mapped[list[int]] = mapped_column(JSONB, default=list)

    email: Mapped[Email] = relationship(back_populates="inquiries")

    __table_args__ = (
        # One letter per supplier per RFQ. Two would be the mistake the
        # grouping exists to prevent, arriving by a different door.
        UniqueConstraint("email_id", "supplier", name="rfq_inquiries_email_id_supplier_key"),
    )


class RfqApproval(Base):
    """That somebody signed off the pricing of this RFQ, and with what.

    One row per email at most, like the verdict and the extraction. Its
    presence is the whole of "this RFQ is approved" - there is no flag beside
    it to disagree with, and no way back.

    The margins sit here rather than on the lines because they are one decision
    about the whole quotation: they answer "why is this line priced so" once,
    instead of repeating the same two numbers down every row.
    """

    __tablename__ = "rfq_approvals"

    email_id: Mapped[str] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Percentages, as a person typed them. `Numeric` for the same reason the
    # prices are: a margin that rounds differently on two machines prices the
    # quotation differently on two machines.
    margin_stock: Mapped[Decimal] = mapped_column(Numeric(7, 3))
    margin_jit: Mapped[Decimal] = mapped_column(Numeric(7, 3))

    email: Mapped[Email] = relationship(back_populates="approval")


class RfqLineCandidate(Base):
    """One product the search offered for a line, and how far behind it ranked.

    Empty for a confirmed code: there was nothing to choose between, and
    offering alternatives would invent a doubt that does not exist.
    """

    __tablename__ = "rfq_line_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_id: Mapped[int] = mapped_column(
        ForeignKey("rfq_lines.id", ondelete="CASCADE"), index=True
    )
    # Position in the shortlist, 1 first. Stored rather than inferred from the
    # score: two candidates can tie, and the order they were shown in is a fact.
    rank: Mapped[int] = mapped_column(Integer)

    item_code: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    # 0-100, how sure matching is that this is what the line asked for. Null
    # where it could not score the line: a dash on the screen, not a zero that
    # would read as "certainly not".
    confidence: Mapped[int | None] = mapped_column(Integer)
    # What the score rests on, in one sentence.
    why: Mapped[str] = mapped_column(Text, default="")
    # The whole row again: the table shows a candidate in the same columns it
    # shows a confirmed product, and it may not have fewer of them just because
    # nothing has been confirmed yet.
    item: Mapped[dict] = mapped_column(JSONB, default=dict)

    line: Mapped[RfqLine] = relationship(back_populates="candidates")

    __table_args__ = (
        UniqueConstraint("line_id", "rank", name="rfq_line_candidates_line_id_rank_key"),
    )
