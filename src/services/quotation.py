"""The quotation a customer receives, as a PDF.

The layout is the desk's own: the SCINT report behind the desk's sample
quotations (`docs/samples/PDFQuote*.pdf`, kept out of git and the image -
they are real customers'), measured off those files and redrawn here - the same page size, the same
Helvetica at the same sizes, the same grey panels in the same places. A
customer who has had quotations from this desk before should not be able to
tell which system this one came out of.

Nothing is decided here and nothing is priced here. Every number arrives
approved, and the only arithmetic is the one the screen already shows -
the approved unit price times the quantity. A document that recomputed from
cost and margin would be quoting a number nobody signed.

Where we have nothing to say, the cell is left blank rather than dropped:
the desk's own quotations leave `Contact` and `Phone` blank the same way, and
a form that changes shape with what we happen to know reads as a different
form.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

logger = logging.getLogger(__name__)

# The desk's page, in points. Not A4 by name, but A4 in proportion (1221.12 /
# 864 is A4's 1.414), so it prints onto A4 whole and at the same size the desk's
# own quotations do.
PAGE = (864.0, 1221.12)
LEFT, RIGHT, TOP, BOTTOM = 31.3, 52.5, 24.0, 48.0
WIDTH = PAGE[0] - LEFT - RIGHT

# The two columns of panels, and the gutter between them.
PANEL_LEFT = 384.2
GUTTER = 18.8
PANEL_RIGHT = WIDTH - PANEL_LEFT - GUTTER

HEADING = colors.Color(0.753, 0.753, 0.753)
RULE = colors.Color(0.827, 0.827, 0.827)
FOOTER = colors.Color(0.45, 0.45, 0.45)

BODY = 8.0
TITLE = 10.0
BANNER = 12.0

# How long a quotation holds. The same thirty days the on-screen preview says,
# counted from the day the prices were approved.
VALID_FOR = timedelta(days=30)

TERMS = "All quotes are subject to General terms and conditions of Seven Seas Group."

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

CENT = Decimal("0.01")

# What the taxed form prints in its discount and VAT columns. Nothing we hold
# says a line is discounted or taxed, and the desk's own UAE quotation prints
# zeros there too; a rate other than zero is an accounting decision, not
# something to default into a customer's document.
DISCOUNT = Decimal("0.00")
VAT_RATE = Decimal("0.00")


@dataclass(frozen=True)
class Issuer:
    """Whose letterhead the quotation goes out on, and which form it uses."""

    name: str
    address: str
    phone: str
    email: str
    web: str
    # Printed beside the address when the office has one to print. The
    # Singapore form carries its UEN there; the Dubai form has no such column.
    registration: str = ""
    # Whether the form carries discount and VAT columns. The Dubai office's
    # does, and a VAT line in its totals; the Singapore office's has neither.
    taxed: bool = False


# The Singapore office, as its own quotations print it.
SINGAPORE = Issuer(
    name="Seven Seas Maritime Services (Singapore) Pte. Ltd",
    address="12 Tuas Road, Singapore. 638486",
    registration="UEN : 199305221C, Tax Reg No : M201169402",
    phone="Phone: +65 3152 2188 Fax: +65 3152 2189",
    email="supply.singapore@sevenseasgroup.com",
    web="www.sevenseasgroup.com",
)

# The Dubai office, as `docs/samples/PDFQuote-UAE.pdf` prints it - with no fax
# number after `Fax:`, which is how the desk's own form has it.
DUBAI = Issuer(
    name="Seven Seas Shipchandlers (L.L.C)",
    address=(
        "Plot 598-668, Dubai Investments Park,Off Emirates Road, P.O.Box 5592, "
        "Dubai, United Arab Emirates"
    ),
    phone="Phone: +971 4 8033 3333 Fax:",
    email="supply.uae@sevenseasgroup.com",
    web="www.sevenseasgroup.com",
    taxed=True,
)


@dataclass(frozen=True)
class QuotedLine:
    """One line, as approved."""

    number: int
    code: str
    description: str
    quantity: str
    uom: str
    unit_price: Decimal | None

    @property
    def total(self) -> Decimal | None:
        """Unit price times quantity, to the cent.

        The shown unit price is what is multiplied - the same rule the screen
        follows, so the document's total agrees with its own column. A
        quantity that is not a number ("2 coil") leaves the line without a
        total rather than with an invented one.
        """
        amount = number_of(self.quantity)
        if self.unit_price is None or amount is None:
            return None
        return (self.unit_price * amount).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def amount(self) -> Decimal | None:
        """The total less the discount, which is always none."""
        return None if self.total is None else self.total - DISCOUNT

    @property
    def tax(self) -> Decimal | None:
        if self.amount is None:
            return None
        return (self.amount * VAT_RATE / 100).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def net(self) -> Decimal | None:
        """What the line comes to on the taxed form, tax included."""
        if self.amount is None or self.tax is None:
            return None
        return self.amount + self.tax


@dataclass(frozen=True)
class Quotation:
    """Everything the document says, and nothing it has to look up."""

    issuer: Issuer
    number: str
    customer: str
    customer_email: str
    port: str
    vessel: str
    imo: str
    received_on: date | None
    approved_at: datetime
    lines: Sequence[QuotedLine] = field(default_factory=tuple)
    currency: str = "USD"

    @property
    def valid_till(self) -> date:
        return self.approved_at.date() + VALID_FOR

    @property
    def subtotal(self) -> Decimal:
        # A line without a total is not counted as zero, the same as on screen.
        # After approval there are none, but the rule stays one rule.
        return sum((line.total for line in self.lines if line.total is not None), Decimal(0))

    @property
    def vat(self) -> Decimal:
        return sum((line.tax for line in self.lines if line.tax is not None), Decimal(0))

    @property
    def grand_total(self) -> Decimal:
        # Freight and other charges are zero on every quotation we issue.
        return self.subtotal + (self.vat if self.issuer.taxed else Decimal(0))


def render(quotation: Quotation, logo: Path | None = None) -> bytes:
    """The quotation as PDF bytes."""
    out = BytesIO()
    document = SimpleDocTemplate(
        out,
        pagesize=PAGE,
        leftMargin=LEFT,
        rightMargin=RIGHT,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        title=f"Quotation {quotation.number}".strip(),
        author=quotation.issuer.name,
    )
    story = [
        _letterhead(quotation.issuer, logo),
        Paragraph(f"Quotation For {_text(quotation.customer)}", _BANNER),
        Spacer(0, 8),
        _pair(
            _panel(
                "Customer Address", [("Billing Address", "")], PANEL_LEFT, label=108, tall=True
            ),
            _panel(
                "Seven Seas Contact Details",
                [("Seven Seas Contact Person", ""), ("Email-Id", quotation.issuer.email)],
                PANEL_RIGHT,
                label=96,
            ),
        ),
        _pair(
            _panel(
                "RFQ Details",
                [
                    # The customer's own RFQ number is still ours to read out
                    # of their subject line; until then it is blank, as on screen.
                    ("Reference", ""),
                    ("Contact", ""),
                    ("Phone", ""),
                    ("Email", quotation.customer_email),
                ],
                PANEL_LEFT,
                label=108,
            ),
            _panel(
                "Port and Dates",
                [
                    ("Port", quotation.port),
                    ("Lead Time (Days)", ""),
                    ("RFQ Received Date", _day(quotation.received_on)),
                    ("Offer Valid Till", _day(quotation.valid_till)),
                ],
                PANEL_RIGHT,
                label=96,
            ),
        ),
        _pair(
            _panel(
                "Vessel Details",
                [("Vessel Name", quotation.vessel), ("IMO Number", quotation.imo)],
                PANEL_LEFT,
                label=108,
            ),
            _panel(
                "Seven Seas Reference",
                [("Quotation Number", quotation.number), ("Seven Seas Client Code", "")],
                PANEL_RIGHT,
                label=96,
            ),
        ),
        _panel(
            "Supplier Terms and Condition",
            [("Comments", TERMS), ("Payment Terms", "")],
            WIDTH,
            label=93,
        ),
        Spacer(0, 10),
        _items(quotation),
        Spacer(0, 10),
        _totals(quotation),
    ]
    document.build(story, canvasmaker=_PagedCanvas)
    return out.getvalue()


def _letterhead(issuer: Issuer, logo: Path | None) -> Table:
    address: Table | Paragraph
    if issuer.registration:
        # The registration sits in a column of its own on the Singapore form,
        # lined up at the same place whatever the address runs to.
        address = Table(
            [
                [
                    Paragraph(_text(issuer.address), _LETTER),
                    Paragraph(_text(issuer.registration), _LETTER),
                ]
            ],
            colWidths=[209.0, WIDTH - 230 - 209.0],
        )
        address.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), *_FLUSH]))
    else:
        address = Paragraph(_text(issuer.address), _LETTER)
    lines = [
        Paragraph(f"<b>{_text(issuer.name)}</b>", _LETTER),
        address,
        Paragraph(_text(issuer.phone), _LETTER),
        Paragraph(f"E-Mail: {_text(issuer.email)} INTERNET: {_text(issuer.web)}", _LETTER),
    ]
    mark = _logo(logo)
    table = Table([[lines, mark]], colWidths=[WIDTH - 230, 230])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "LEFT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _logo(path: Path | None) -> Image | str:
    """The mark, at the size the desk prints it. Blank if the file is missing -
    a quotation without a logo is still a quotation, one that fails to render
    is not."""
    if path is None:
        return ""
    # Checked up front: ReportLab defers opening the file until it draws the
    # page, and by then a missing one fails the whole build.
    if not path.is_file():
        logger.warning("Quotation logo %s is missing; rendering without it", path)
        return ""
    try:
        return Image(str(path), width=187.5, height=43.5)
    except OSError:
        logger.warning("Quotation logo %s could not be read; rendering without it", path)
        return ""


def _panel(
    title: str,
    rows: Sequence[tuple[str, str]],
    width: float,
    *,
    label: float,
    tall: bool = False,
) -> Table:
    """A grey heading over label/value rows, as every block of the form is."""
    data = [[Paragraph(_text(title), _TITLE), ""]]
    data += [
        [Paragraph(_text(name), _LABEL), Paragraph(_text(value), _BODY)] for name, value in rows
    ]
    # The billing address gets room for three lines, as on the desk's form,
    # whether or not there is anything to put there.
    heights = [None] * len(data)
    if tall:
        heights[-1] = 32
    table = Table(data, colWidths=[label, width - label], rowHeights=heights)
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), HEADING),
                ("GRID", (0, 0), (-1, -1), 0.5, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _pair(left: Table, right: Table) -> Table:
    table = Table([[left, "", right]], colWidths=[PANEL_LEFT, GUTTER, PANEL_RIGHT])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return table


# Sr No, Identification, Description, Qty, Uom, UnitPrice, Total. Both desk
# forms have a pack size between Uom and the price; it is left out, as it is
# on the screen, and its width goes to the description.
_COLUMNS = [59.9, 90.9, 329.3, 66.8, 62.0, 86.6, 84.7]
_NAMES = ["Sr No", "Identification", "Description", "Qty", "Uom", "UnitPrice", "Total"]

# The Dubai form: Sr No, Identification, Description, Qty, Uom, Rate,
# Discount, Amount, VAT%, Tax Amount, Net Total - measured off
# `docs/samples/PDFQuote-UAE.pdf`, with its `Pack Size` width given to the description.
_TAXED_COLUMNS = [33.3, 64.2, 276.7, 41.6, 36.3, 59.7, 40.9, 66.7, 41.0, 52.7, 67.1]
_TAXED_NAMES = [
    "Sr No",
    "Identification",
    "Description",
    "Qty",
    "Uom",
    "Rate",
    "Discount ( - )",
    "Amount",
    "VAT%",
    "Tax Amount",
    "Net Total",
]


def _cells(line: QuotedLine, taxed: bool) -> list[Paragraph]:
    first = [
        Paragraph(str(line.number), _BODY_CENTER),
        Paragraph(_text(line.code), _BODY),
        Paragraph(_text(line.description), _BODY),
        Paragraph(_text(_quantity(line.quantity)), _BODY_RIGHT),
        Paragraph(_text(line.uom), _BODY),
        Paragraph(_money(line.unit_price), _BODY_RIGHT),
    ]
    if not taxed:
        return [*first, Paragraph(_money(line.total), _BODY_RIGHT)]
    return [
        *first,
        Paragraph(_money(DISCOUNT), _BODY_RIGHT),
        Paragraph(_money(line.amount), _BODY_RIGHT),
        Paragraph(f"{_money(VAT_RATE)}%", _BODY_RIGHT),
        Paragraph(_money(line.tax), _BODY_RIGHT),
        Paragraph(_money(line.net), _BODY_RIGHT),
    ]


def _items(quotation: Quotation) -> Table:
    taxed = quotation.issuer.taxed
    widths = _TAXED_COLUMNS if taxed else _COLUMNS
    names = _TAXED_NAMES if taxed else _NAMES
    last = len(widths) - 1
    # Three grey blocks, as on both desk forms: the title over the text
    # columns, an empty one over `Qty`, and the currency over the numbers.
    heading: list[Paragraph | str] = [""] * len(widths)
    heading[0] = Paragraph("Line Items", _TITLE_LEFT)
    heading[4] = Paragraph(f"Currency: {_text(quotation.currency)}", _TITLE_RIGHT)
    columns = [Paragraph(name, _LABEL if i == 0 else _LABEL_CENTER) for i, name in enumerate(names)]
    rows = [_cells(line, taxed) for line in quotation.lines]
    # Both heading rows repeat on every page: a second page of numbers with no
    # column names over them is a page nobody can read on its own.
    table = Table([heading, columns, *rows], colWidths=widths, repeatRows=2)
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (2, 0)),
                ("SPAN", (4, 0), (last, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), HEADING),
                ("GRID", (0, 1), (-1, -1), 0.5, RULE),
                ("LINEAFTER", (2, 0), (3, 0), 1, colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _totals(quotation: Quotation) -> Table:
    data = [
        [Paragraph("Subtotal", _TITLE_LEFT), Paragraph(_money(quotation.subtotal), _TITLE_RIGHT)],
        # Freight and other charges are not something this desk quotes yet;
        # zero is what its own quotations print when there are none.
        [Paragraph("Freight ( + )", _BODY), Paragraph("0.00", _BODY_RIGHT)],
        [Paragraph("Other ( + )", _BODY), Paragraph("0.00", _BODY_RIGHT)],
    ]
    if quotation.issuer.taxed:
        data.append([Paragraph("VAT", _BODY), Paragraph(_money(quotation.vat), _BODY_RIGHT)])
    data.append(
        [
            Paragraph(f"Total Price({_text(quotation.currency)})", _TITLE_LEFT),
            Paragraph(_money(quotation.grand_total), _TITLE_RIGHT),
        ]
    )
    inner = Table(data, colWidths=[181.5, PANEL_RIGHT - 181.5])
    inner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEADING),
                ("GRID", (0, 0), (-1, -1), 0.5, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    outer = Table([["", "", inner]], colWidths=[PANEL_LEFT, GUTTER, PANEL_RIGHT])
    outer.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return outer


class _PagedCanvas(Canvas):
    """Holds every page back until the last, so each can say `Page n of N`."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self) -> None:  # noqa: N802 - ReportLab's own name
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        count = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont("Helvetica", BODY)
            self.setFillColor(FOOTER)
            self.drawRightString(PAGE[0] - RIGHT, 34, f"Page {self._pageNumber} of {count}")
            super().showPage()
        super().save()


def _text(value: str) -> str:
    """Plain text made safe for a ReportLab paragraph, which reads markup: a
    customer's `<` or `&` must print, not open a tag."""
    return escape(value or "")


def number_of(text: str) -> Decimal | None:
    """A quantity as a number, on the screen's rule: spaces out, a comma is a
    decimal point, and only a positive amount counts."""
    cleaned = (text or "").replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _quantity(text: str) -> str:
    """`8.00`, as the desk prints it; the customer's own words when they are
    not a number, rather than a blank that hides what was asked."""
    amount = number_of(text)
    return f"{amount.quantize(CENT, rounding=ROUND_HALF_UP)}" if amount is not None else text


def _money(amount: Decimal | None) -> str:
    return "" if amount is None else f"{amount.quantize(CENT, rounding=ROUND_HALF_UP)}"


def _day(value: date | None) -> str:
    """`01 Aug 2026`. Month names are spelled here rather than by the locale,
    so the server's language settings cannot change a customer's document."""
    return f"{value.day:02d} {MONTHS[value.month - 1]} {value.year}" if value else ""


_FLUSH = [
    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ("TOPPADDING", (0, 0), (-1, -1), 0),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
]

_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=BODY, leading=BODY + 1.2)
# The letterhead is set looser than the tables, 13 pt a line, as the desk's is.
_LETTER = ParagraphStyle("letter", parent=_BODY, leading=13.0)
_BODY_RIGHT = ParagraphStyle("body-right", parent=_BODY, alignment=TA_RIGHT)
_BODY_CENTER = ParagraphStyle("body-center", parent=_BODY, alignment=TA_CENTER)
_LABEL = ParagraphStyle("label", parent=_BODY, fontName="Helvetica-Bold")
_LABEL_CENTER = ParagraphStyle("label-center", parent=_LABEL, alignment=TA_CENTER)
_TITLE = ParagraphStyle(
    "title", fontName="Helvetica-Bold", fontSize=TITLE, leading=TITLE + 2, alignment=TA_CENTER
)
_TITLE_LEFT = ParagraphStyle("title-left", parent=_TITLE, alignment=TA_LEFT)
_TITLE_RIGHT = ParagraphStyle("title-right", parent=_TITLE, alignment=TA_RIGHT)
_BANNER = ParagraphStyle(
    "banner",
    fontName="Helvetica-Bold",
    fontSize=BANNER,
    leading=BANNER + 3,
    alignment=TA_CENTER,
    spaceBefore=4,
)
