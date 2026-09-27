"""The quotation as the desk's own Excel workbook - `Quote.xlsm`.

The file SCINT exports for an office: a `SUMMARY` sheet with the office's
details and the totals, and a second sheet, named after the quotation, with
one row per line. It carries a VBA project behind a `Print` button, a logo,
sheet protection and printer settings, and openpyxl drops the button and the
printer settings on the way through. So the workbook is edited the way the
agent's own form is (`src.infrastructure.excel`): as the zip it is, with the
cells we own rewritten in place and every other byte passed through.

What is written:

* **The office** - location, address, phone, email, tax number, payment
  terms - from `Office`, fixed per letterhead.
* **The quotation** - vessel, port, date, client, our number - from the
  approved RFQ.
* **One row per line**, copied from the template's model row 11: its styles,
  and its formulas with the row number moved. The totals stay formulas too,
  over the rows actually written, so the book still recalculates when the
  desk changes a quantity - the same live sheet the desk is used to.

Every formula also gets its cached value. Excel recomputes on open anyway
(`fullCalcOnLoad`), but a previewer - Quick Look, a mail client, a browser -
shows the cache, and a cache of zeros would show a quotation for nothing.
"""

import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

from src.services.quotation import CENT, QuotedLine, number_of

SUMMARY = "xl/worksheets/sheet1.xml"
DETAILS = "xl/worksheets/sheet2.xml"
WORKBOOK = "xl/workbook.xml"
CORE = "docProps/core.xml"
SHARED = "xl/sharedStrings.xml"

# The row the template's one line sits on, and which every line is copied from.
MODEL_ROW = 11
# Every column the model row has, `A` to `S`. `M` and `N` are hidden helper
# columns, `P` to `S` the discount and VAT working.
COLUMNS = [chr(code) for code in range(ord("A"), ord("S") + 1)]

# What the template's discount cell holds. Not zero - a zero there switches the
# discount lines off through the sheet's own conditional formats - but so small
# that no amount comes out different.
DISCOUNT = Decimal("1E-13")

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# What Excel refuses in a sheet name, and the most it allows.
_NOT_IN_A_SHEET_NAME = re.compile(r"[\[\]:*?/\\']")
MOST_A_SHEET_NAME_RUNS = 31
FALLBACK_SHEET = "QUOTE"

# XML 1.0 has no way to encode these, and customer text does contain them.
_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class Office:
    """The office block of the `SUMMARY` sheet. Fixed per letterhead."""

    location: str
    address: str
    phone: str
    email: str
    tax_reg: str
    payment_terms: str = ""


# As the desk's own UAE workbook has it, word for word.
DUBAI_OFFICE = Office(
    location="Seven Seas Shipchandlers (L.L.C) (Dubai)",
    address=(
        "Plot 598-668, Dubai Investments Park,Off Emirates Road, P.O.Box 5592, "
        "Dubai, United Arab Emirates"
    ),
    phone="Ph: +971 4 8033 3333",
    email="supply.uae@sevenseasgroup.com",
    tax_reg="100569476300003",
    payment_terms="60 DAYS FROM THE DATE OF INVOICE",
)

# The Singapore office, in the lines its own PDF letterhead prints. No
# payment terms: none of the office's own quotations names any.
SINGAPORE_OFFICE = Office(
    location="Seven Seas Maritime Services (Singapore) Pte. Ltd (Singapore)",
    address="12 Tuas Road, Singapore. 638486 UEN : 199305221C, Tax Reg No : M201169402",
    phone="Phone: +65 3152 2188 Fax: +65 3152 2189",
    email="E-Mail: supply.singapore@sevenseasgroup.com INTERNET: www.sevenseasgroup.com",
    tax_reg="M201169402",
)


@dataclass(frozen=True)
class BookLine(QuotedLine):
    """One line, with the customer's side of it as our sheet records it."""

    customer_code: str = ""
    customer_description: str = ""


@dataclass(frozen=True)
class QuoteBook:
    """Everything the workbook says."""

    office: Office
    number: str
    vessel: str
    port: str
    client: str
    quoted_on: date | None
    lines: Sequence[BookLine] = field(default_factory=tuple)
    currency: str = "USD"

    @property
    def gross(self) -> Decimal:
        return sum((line.total for line in self.lines if line.total is not None), Decimal(0))


def _discount(gross: Decimal) -> Decimal:
    """The template's near-zero discount on this gross, as Excel shows it: to
    the cent. The cache is what a previewer prints, and `5.6E-10` under
    `Total Discount` would read as a number somebody has to explain."""
    return (gross * DISCOUNT).quantize(CENT)


def sheet_name(number: str) -> str:
    """The line sheet's name: our number, with what Excel refuses taken out."""
    cleaned = _NOT_IN_A_SHEET_NAME.sub("", number).strip()[:MOST_A_SHEET_NAME_RUNS]
    return cleaned if cleaned and cleaned.upper() != "SUMMARY" else FALLBACK_SHEET


def fill(template: bytes, book: QuoteBook) -> bytes:
    """A copy of the template with this quotation written into it."""
    with zipfile.ZipFile(io.BytesIO(template)) as master:
        old_name = _details_name(master.read(WORKBOOK).decode("utf-8"))
        new_name = sheet_name(book.number)
        parts = {
            SUMMARY: _summary(master.read(SUMMARY).decode("utf-8"), book, old_name, new_name),
            DETAILS: _details(master.read(DETAILS).decode("utf-8"), book),
            WORKBOOK: _workbook(master.read(WORKBOOK).decode("utf-8"), old_name, new_name),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
            for entry in master.infolist():
                if entry.filename in parts:
                    copy.writestr(entry, parts[entry.filename].encode("utf-8"))
                else:
                    copy.writestr(entry, master.read(entry.filename))
    return buffer.getvalue()


# --- the two sheets --------------------------------------------------------


def _summary(xml: str, book: QuoteBook, old_name: str, new_name: str) -> str:
    office = book.office
    gross = book.gross
    discount = _discount(gross)
    net = gross - discount
    xml = _repointed(xml, old_name, new_name)
    for reference, value in {
        "C4": book.vessel,
        "C5": book.port,
        "C7": book.number,
        "C8": _long_day(book.quoted_on),
        "C9": office.location,
        "C10": office.address,
        "C13": office.phone,
        "C14": office.email,
        "C15": office.tax_reg,
        "C17": book.client,
        # We hold no tax number for a customer; blank rather than guessed.
        "C19": "",
        "C20": office.payment_terms,
    }.items():
        xml = _set(xml, reference, value)
    # The department row and the totals, cached as Excel would compute them.
    cached: dict[str, str | Decimal] = {
        "H4": book.currency,
        "G7": book.number,
        "H7": "",
        "J7": net,
        "K7": discount,
        "L7": gross,
        "M7": Decimal(0),
        "N7": Decimal(0),
        "I18": book.currency,
        "I19": book.currency,
        "I20": book.currency,
        "I21": book.currency,
        "I22": book.currency,
        "I23": book.currency,
        "J18": gross,
        "J19": discount,
        "J20": net,
        "J21": Decimal(0),
        "J22": Decimal(0),
        "J23": net,
    }
    for reference, value in cached.items():
        xml = _cache(xml, reference, value)
    return xml


def _details(xml: str, book: QuoteBook) -> str:
    gross = book.gross
    discount = _discount(gross)
    first, last = MODEL_ROW, MODEL_ROW + max(len(book.lines), 1) - 1
    for reference, value in {
        "B1": book.number,
        "K1": book.vessel,
        # The customer's own RFQ number; we do not hold it yet.
        "D3": "",
    }.items():
        xml = _set(xml, reference, value)
    for reference in ("K5", "K6", "K7", "K8"):
        xml = _set(xml, reference, book.currency)

    xml = _formula(xml, "L3", f"SUM(N{first}:N{last})", gross)
    xml = _cache(xml, "L4", discount)
    xml = _cache(xml, "L5", gross - discount)
    xml = _formula(xml, "L6", f"SUM(S{first}:S{last})", Decimal(0))
    xml = _cache(xml, "L8", gross - discount)
    xml = _cache(xml, "K10", f"Rate ({book.currency})")
    xml = _cache(xml, "L10", f"Gross Total ({book.currency})")

    model = _row(xml, MODEL_ROW)
    styles = dict(re.findall(r'<c r="([A-Z]+)\d+"[^>]*?\ss="(\d+)"', model))
    rows = [_line_row(line, MODEL_ROW + offset, styles) for offset, line in enumerate(book.lines)]
    if not rows:
        # An empty quotation keeps the model row, emptied, so the sheet still
        # has the look the next file is copied from.
        rows = [_empty_row(MODEL_ROW, model, styles)]
    return xml.replace(model, "".join(rows), 1)


def _line_row(line: BookLine, row: int, styles: dict[str, str]) -> str:
    quantity = number_of(line.quantity)
    total = line.total
    price = line.unit_price
    cells = {
        "A": _number(line.number),
        "B": _text(line.customer_code),
        "C": _text(line.code),
        "D": _text(line.customer_description),
        # Country of origin and brand: nothing we hold says either.
        "E": _text(""),
        "F": _text(""),
        "G": _text(line.description),
        # The supplier's remarks on the offer; there are none to carry.
        "H": _text(""),
        "I": _number(quantity) if quantity is not None else _text(line.quantity),
        "J": _text(line.uom),
        "K": _number(price) if price is not None else "",
        "L": _formula_body(f"N{row}", total),
        # A hidden copy of the quantity, as the desk's own rows carry.
        "M": _number(quantity) if quantity is not None else "",
        "N": _formula_body(f"K{row}*I{row}", total),
        "O": "",
        "P": _formula_body(f"(K{row}*(K4))*I{row}", Decimal(0) if total is not None else None),
        "Q": _formula_body(f"(K{row}*I{row}) -P{row}", total),
        "R": _number(Decimal(0)),
        "S": _formula_body(
            f"(Q{row}*(1+R{row}))-(Q{row})", Decimal(0) if total is not None else None
        ),
    }
    body = "".join(_cell(f"{column}{row}", styles.get(column), cells[column]) for column in COLUMNS)
    return f'<row r="{row}" customFormat="1" s="79">{body}</row>'


def _empty_row(row: int, model: str, styles: dict[str, str]) -> str:
    body = "".join(_cell(f"{column}{row}", styles.get(column), "") for column in COLUMNS)
    opening = re.match(r"<row\b[^>]*>", model)
    return f"{opening.group(0) if opening else f'<row r={row!r}>'}{body}</row>"


# --- the workbook ------------------------------------------------------------


def _details_name(workbook: str) -> str:
    names = re.findall(r'<sheet\b[^>]*\bname="([^"]*)"', workbook)
    if len(names) < 2:
        raise ValueError("the quote template has no line sheet")
    return _unescape(names[1])


def _workbook(xml: str, old_name: str, new_name: str) -> str:
    xml = xml.replace(f'name="{escape(old_name)}"', f'name="{escape(new_name)}"', 1)
    # Recalculate on open: the cached values are ours, the formulas are Excel's.
    if "fullCalcOnLoad" not in xml:
        xml = re.sub(r"<calcPr\b", '<calcPr fullCalcOnLoad="1"', xml, count=1)
    return xml


def _repointed(xml: str, old_name: str, new_name: str) -> str:
    """The summary's formulas, pointed at the renamed line sheet."""
    return xml.replace(f"'{escape(old_name)}'!", f"'{escape(new_name)}'!")


# --- cells -------------------------------------------------------------------


def _cell_pattern(reference: str) -> re.Pattern[str]:
    return re.compile(
        rf'<c r="{reference}"(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</c>)', re.DOTALL
    )


def _find(xml: str, reference: str) -> re.Match[str]:
    match = _cell_pattern(reference).search(xml)
    if match is None:
        raise ValueError(f"the quote template has no cell {reference}")
    return match


def _style_of(attrs: str) -> str | None:
    found = re.search(r'\ss="(\d+)"', attrs)
    return found.group(1) if found else None


def _set(xml: str, reference: str, value: str) -> str:
    """Write text into an existing cell, keeping its style."""
    match = _find(xml, reference)
    replacement = _cell(reference, _style_of(match.group("attrs")), _text(value))
    return xml[: match.start()] + replacement + xml[match.end() :]


def _formula(xml: str, reference: str, formula: str, cached: Decimal | str | None) -> str:
    match = _find(xml, reference)
    replacement = _cell(reference, _style_of(match.group("attrs")), _formula_body(formula, cached))
    return xml[: match.start()] + replacement + xml[match.end() :]


def _cache(xml: str, reference: str, cached: Decimal | str) -> str:
    """Keep the cell's formula, and give it the value Excel would compute."""
    match = _find(xml, reference)
    found = re.search(r"<f\b[^>]*>(.*?)</f>", match.group("body") or "", re.DOTALL)
    if found is None:
        raise ValueError(f"the quote template's {reference} is not a formula")
    replacement = _cell(
        reference,
        _style_of(match.group("attrs")),
        _formula_body(_unescape(found.group(1)), cached),
    )
    return xml[: match.start()] + replacement + xml[match.end() :]


def _cell(reference: str, style: str | None, content: str) -> str:
    """One `<c>`. `content` is what `_text`, `_number` or `_formula_body` made -
    each starts with the cell's type attribute, if it needs one."""
    styled = f' s="{style}"' if style is not None else ""
    if not content:
        return f'<c r="{reference}"{styled}/>'
    kind, _, inner = content.partition("|")
    return f'<c r="{reference}"{styled}{kind}>{inner}</c>'


def _text(value: str) -> str:
    cleaned = _FORBIDDEN.sub("", value or "")
    if not cleaned:
        return ""
    return f' t="inlineStr"|<is><t xml:space="preserve">{escape(cleaned)}</t></is>'


def _number(value: int | Decimal | None) -> str:
    return "" if value is None else f"|<v>{_plain(value)}</v>"


def _formula_body(formula: str, cached: Decimal | str | None) -> str:
    expression = f"<f>{escape(formula)}</f>"
    if cached is None:
        return f"|{expression}"
    if isinstance(cached, str):
        return f' t="str"|{expression}<v>{escape(cached)}</v>'
    return f"|{expression}<v>{_plain(cached)}</v>"


def _plain(value: int | Decimal) -> str:
    """A number as Excel writes one: no exponent, no trailing zeros."""
    if isinstance(value, int):
        return str(value)
    text = format(value.normalize(), "f")
    return text if text not in ("-0", "") else "0"


def _row(xml: str, number: int) -> str:
    match = re.search(rf'<row r="{number}"[^>]*>.*?</row>', xml, re.DOTALL)
    if match is None:
        raise ValueError(f"the quote template has no row {number}")
    return match.group(0)


def _long_day(value: date | None) -> str:
    """`01 September 2026`, as the desk's workbook writes its date - spelled
    here so the server's locale cannot change a customer's document."""
    return f"{value.day:02d} {MONTHS[value.month - 1]} {value.year}" if value else ""


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    )
