"""Fill the RFQ form with a sample and say where everything went.

    uv run python -m src.tools.fill_template
    uv run python -m src.tools.fill_template --out /tmp/rfq.xlsx

The point is the same as `inspect_attachments`: eyes on the output. Unit tests
confirm that a value reaches a cell; only opening the file in Excel confirms
that Excel is happy to open it, and that question comes back every time the
desk sends a new form. Costs nothing - no model is called and no email is read.
"""

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from src.core.config import get_settings
from src.infrastructure.excel import Template
from src.services.extraction import RfqExtraction
from src.services.extraction.models import (
    HeaderField,
    HeaderValue,
    LineItem,
    NormalizedHeader,
    RfqHeader,
)
from src.services.workbook import CELLS, WorkbookBuilder

BRANCH = "UAE-DUBAI"
RECEIVED = datetime.now(UTC).replace(microsecond=0)

# A believable RFQ, deliberately incomplete: `etd` and the delivery address are
# missing, because a filled-in copy of the form is not what a person needs to
# look at - a half-filled one is.
HEADER: dict[HeaderField, str] = {
    HeaderField.VESSEL_NAME: "MV ALMI GLOBE",
    HeaderField.SENDER_CODE: "AE1188",
    HeaderField.IMO: "9232395",
    HeaderField.RFQ_REFERENCE: "78432",
    HeaderField.CUSTOMER_CONTACT: "Nikos Papadopoulos",
    HeaderField.CUSTOMER_PHONE: "+30 210 4599 000",
    HeaderField.CUSTOMER_EMAIL: "purchasing@almiship.com",
    HeaderField.PERSON_DESIGNATION: "Purchasing Officer",
    HeaderField.DELIVERY_PORT: "UAE - JEBEL ALI",
    HeaderField.CURRENCY: "AED",
    HeaderField.RFQ_TYPE: "DECK",
}

DATES: dict[HeaderField, date] = {
    HeaderField.ETA: date(2026, 10, 12),
    HeaderField.QUOTE_BEFORE: date(2026, 10, 8),
    HeaderField.REQUESTED_DELIVERY: date(2026, 10, 13),
}

ITEMS = [
    ("550101", "ROPE POLYPROPYLENE 24MM X 220M", "2", "COIL"),
    ("512201", "PAINT MARINE WHITE, 20 LTR", "5", "CAN"),
    ("", "GASKET SET FOR BALLAST PUMP, MAKER SPEC", "1", "SET"),
    ("311101", "SAFETY HELMET WHITE, EN397", "12", "PCS"),
    # Not a plain number, so it goes in as the customer wrote it.
    ("", "GREASE EP2 LITHIUM", "2 x 18KG", "PAIL"),
]


def sample() -> RfqExtraction:
    return RfqExtraction(
        header=RfqHeader(
            fields={
                name: HeaderValue(value=value, source="email.body")
                for name, value in HEADER.items()
            }
        ),
        normalized=NormalizedHeader(text=dict(HEADER), dates=dict(DATES)),
        items=[
            LineItem(
                sr_no=number,
                customer_item_code=code or None,
                description=description,
                quantity=quantity,
                uom=uom,
                source="Requisition_78432.xlsx#Deck",
            )
            for number, (code, description, quantity, uom) in enumerate(ITEMS, start=1)
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--template", type=Path, help="form to fill, defaults to RFQ_TEMPLATE_PATH"
    )
    parser.add_argument("--out", type=Path, default=Path("data/workbooks/sample.xlsx"))
    args = parser.parse_args(argv)

    path = args.template or get_settings().RFQ_TEMPLATE_PATH
    if not path.is_file():
        print(f"No form at {path}", file=sys.stderr)
        return 2

    extraction = sample()
    filled = asyncio.run(
        WorkbookBuilder(Template.load(path)).build(
            extraction, branch=BRANCH, received_at=RECEIVED
        )
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(filled.data)

    _report(extraction, filled, args.out)
    return 0


def _report(extraction: RfqExtraction, filled, out: Path) -> None:
    print(f"\n{filled.filename}  ->  {out}  ({len(filled.data) / 1024:.0f} KB)\n")
    print(f"  {'CELL':5s}  {'FIELD':20s}  VALUE")
    print(f"  {'-' * 5}  {'-' * 20}  {'-' * 40}")
    for name, cell in CELLS.items():
        value = extraction.normalized.text.get(name) or extraction.normalized.dates.get(name)
        print(f"  {cell:5s}  {name.value:20s}  {value if value is not None else '(blank)'}")

    print(f"\n  {filled.items} item(s) from row 24")
    for item in extraction.items[: filled.items]:
        print(f"    {item.sr_no:>3} | {item.customer_item_code or '':10s} | "
              f"{item.description[:40]:40s} | {item.quantity or '':10s} | {item.uom or ''}")

    if filled.missing_required:
        print(f"\n  starred cells left blank: {', '.join(filled.missing_required)}")
    print("\n  --- remarks written into A14 ---")
    for line in _remarks_of(out).split("\n"):
        print(f"   {line}")
    print()


def _remarks_of(path: Path) -> str:
    import openpyxl

    book = openpyxl.load_workbook(path)
    remarks = book["KASS RFQ Template"]["A14"].value or ""
    book.close()
    return remarks


if __name__ == "__main__":
    raise SystemExit(main())
