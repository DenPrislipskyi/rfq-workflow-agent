"""Build a second set of seven RFQ test cases out of the real product catalogue.

    uv run python test_cases_2/build.py

`test_cases/` covers the shape of the pipeline: is this an RFQ, can the files be
opened, does the form get filled. This set covers the part that changed - the
three matching branches, the 80% threshold that chooses between them, and the
places where the sheet itself is the problem.

Each folder is one email as a person would have sent it, the files that came
with it, and `EXPECTED.md` saying what should happen and why. The products come
out of `data/catalog/items.csv`, so a case that should match really can and a
case that should not really cannot.

Run `test_cases_2/run.py <n>` to push one case through the real pipeline.
"""

import csv
import io
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402

from src.core.config import get_settings  # noqa: E402
from src.domain.rules.catalog import Catalog, CatalogItem, normalize_code, tokenize  # noqa: E402
from src.infrastructure.catalog import Snapshot  # noqa: E402
from tests.attachments_builder import pdf_with_text  # noqa: E402

HERE = Path(__file__).parent
MAILBOX = "supply@sevenseas.example.com"

# The code the sheet files under two different item codes: heat shrink tubing
# in two sizes. Everything about "one code, several products" hangs off it.
TUBING = "79 54 96"
# One item code under two customer wordings, two sizes apart.
BOILERSUIT = "312374"


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a requisition, as the customer would write it."""

    code: str
    description: str
    quantity: str
    uom: str


def main() -> int:
    settings = get_settings()
    catalog = Catalog.from_rows(
        Snapshot(settings.CATALOG_SNAPSHOT_PATH).rows(),
        code_column=settings.CATALOG_CODE_COLUMN,
        description_column=settings.CATALOG_DESCRIPTION_COLUMN,
        customer_code_column=settings.CATALOG_CUSTOMER_CODE_COLUMN,
        customer_description_column=settings.CATALOG_CUSTOMER_DESCRIPTION_COLUMN,
    )
    if not len(catalog):
        print("No catalogue. Run `python -m src.tools.sync_catalog` first.")
        return 1

    shelf = _shelf(catalog)
    if len(shelf) < 20:
        print(f"Only {len(shelf)} usable products in the catalogue - need at least 20.")
        return 1

    for build in (_case_1, _case_2, _case_3, _case_4, _case_5, _case_6, _case_7):
        folder = build(shelf, catalog)
        files = sorted(p.name for p in folder.iterdir())
        print(f"{folder.name:34} {len(files)} file(s): {', '.join(files)}")

    print(f"\nSeven cases under {HERE}. Run one with: uv run python test_cases_2/run.py 1")
    return 0


def _shelf(catalog: Catalog) -> list[CatalogItem]:
    """Products whose own recorded wording finds them.

    The sheet also holds deliberate bad mappings and placeholder rows; a case
    built on one of those would be testing the fixture, not the pipeline.
    """
    found = []
    for item in catalog.items:
        if not (item.customer_code and item.customer_description):
            continue
        # The sheet's placeholder rows say "Not visible" where a value was not
        # legible. A case built on one of those is testing the fixture.
        if item.customer_description.lower().startswith("not "):
            continue
        if item.customer_code.lower().startswith("not "):
            continue
        best = catalog.search(item.customer_description, limit=1)
        if best and best[0].item.code == item.code:
            found.append(item)
    return found


def _pick(shelf: list[CatalogItem], needle: str) -> CatalogItem | None:
    """The first product whose recorded wording contains this text."""
    return next((i for i in shelf if needle.lower() in i.customer_description.lower()), None)


def _agreement(ours: str, theirs: str) -> int:
    """The pipeline's own measure, so `EXPECTED.md` can state a real number."""
    mine, yours = set(tokenize(ours)), set(tokenize(theirs))
    return round(100 * len(mine & yours) / len(mine)) if mine else 0


# --- the cases ------------------------------------------------------------


def _case_1(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """The 80% threshold, from both sides, in one email."""
    bolt = _pick(shelf, "M16*65")
    goggles = _pick(shelf, "WELDING GOGGLES")
    gloves = _pick(shelf, "five fingers")
    assert bolt and goggles and gloves

    lines = [
        # Word for word what the sheet has. Nothing can push this under 80%.
        Line(bolt.customer_code, bolt.customer_description, "250", "set"),
        # The same product, said a completely different way. The code names the
        # right item and the words will not vouch for it.
        Line(
            bolt.customer_code,
            "M16 diameter 65 long threaded fastener c/w hexagon nut, zinc free, "
            "marine grade, supplied in boxes of one hundred",
            "250",
            "set",
        ),
        Line(goggles.customer_code, goggles.customer_description, "20", "pcs"),
        Line(gloves.customer_code, "gauntlets for welding, leather, 5 digit", "10", "prs"),
    ]
    folder = _folder("1_threshold_edge")

    _email(
        folder,
        subject="RFQ 91004 / MV EASTERN DAWN / Fujairah",
        sender="purchasing@easterndawn.example.com",
        body=(
            "Dear Supply,\n\n"
            "Please quote the attached for MV EASTERN DAWN, IMO 9310129,\n"
            "delivery Fujairah, ETA 04 Nov. Quotation required by 30 Oct.\n\n"
            "Some of the lines below are written the way our engineers describe\n"
            "them rather than the way they appear in your catalogue.\n\n"
            "Regards,\nPurchasing"
        ),
    )
    (folder / "Requisition.xlsx").write_bytes(_xlsx(lines, header=True))

    _expected(
        folder,
        what=(
            "The same product asked for twice, once in the sheet's own words and once in the "
            "customer's. The code is identical on both lines, so the only thing that can tell "
            "them apart is the 80% agreement between our restated line and the sheet's "
            "`Customer Description`."
        ),
        checks=[
            "line 1: wording matches the sheet -> `code_confirmed`, one product, confidence 100, no candidates",
            "line 2: SAME code, wording rewritten -> `code_rejected`, and a top-5 instead",
            "line 2's top-5 should still put the M16 x 65 bolt first - the code was dropped, the product was not",
            "line 3: wording matches -> `code_confirmed`",
            "line 4: reworded gloves -> `code_rejected` with a shortlist",
            "the two bolt lines must not come out the same way. If they do, the threshold is doing nothing",
        ],
        lines=lines,
        notes=[
            f"line 1 agreement with `{bolt.customer_code}`: **{_agreement(lines[0].description, bolt.customer_description)}%** (verbatim, before the model restates it)",
            f"line 2 agreement with `{bolt.customer_code}`: **{_agreement(lines[1].description, bolt.customer_description)}%**",
            f"line 4 agreement with `{gloves.customer_code}`: **{_agreement(lines[3].description, gloves.customer_description)}%**",
            "The model restates every line before this is measured, so the numbers above are the floor, not the answer.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines],
    )
    return folder


def _case_2(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """One customer code, more than one product behind it."""
    del shelf
    tubing = [i for i in catalog.items if normalize_code(i.customer_code) == normalize_code(TUBING)]
    suits = [i for i in catalog.items if normalize_code(i.customer_code) == normalize_code(BOILERSUIT)]

    lines = [
        # The sheet files this code under two item codes, one per size. Asking
        # for size 2 should not be answered with size 1-0.5.
        Line(TUBING, "Heat shrink plastic tubing, nominal size 2, black", "20", "mtr"),
        Line(TUBING, "Heat shrink plastic tubing, nominal size 1-0.5, black", "20", "mtr"),
        # One item code, two wordings, two sizes. Whichever wins, it is the
        # same product code - so the size is lost either way, and the record
        # has to make that visible.
        Line(BOILERSUIT, "Boilersuit 100% cotton navy 2XL", "6", "pcs"),
    ]
    folder = _folder("2_one_code_many_products")

    _email(
        folder,
        subject="RFQ / MT SILVER STRAIT / Singapore / electrical stores",
        sender="stores@silverstrait.example.com",
        body=(
            "Good day,\n\n"
            "Kindly quote the attached for MT SILVER STRAIT, IMO 9445176,\n"
            "delivery Singapore, ETA 11 Nov.\n\n"
            "IMPA codes are as printed in our stores book.\n\n"
            "Stores Officer"
        ),
    )
    (folder / "Stores request.xlsx").write_bytes(_xlsx(lines, header=True))

    _expected(
        folder,
        what=(
            f"`{TUBING}` is filed against two different item codes in the sheet, one per size, and "
            f"`{BOILERSUIT}` is filed twice against one item code in two sizes. Both are real rows of "
            "the customer's own sheet, and both are places where a code alone cannot answer."
        ),
        checks=[
            f"`by_code` resolves `{TUBING}` to the FIRST row it finds, so lines 1 and 2 are handed the same item",
            "line 1 asks for size 2 and line 2 for size 1-0.5 - they must not both come back confirmed on the same code",
            "whichever line disagrees should be `code_rejected` and get a shortlist",
            "a shortlist for these lines is close to useless: both rows' `Item Description` reads "
            "`Not visible / not provided`, so there is nothing for the table to show",
            f"line 3: `{BOILERSUIT}` names one item code under two sizes - the size the customer asked for is not in the answer either way",
            "this case is expected to look bad. It documents a limit of the sheet, not a bug in the code",
        ],
        lines=lines,
        notes=[
            f"`{TUBING}` -> " + ", ".join(f"`{i.code}` ({i.customer_description[:44]})" for i in tubing),
            f"`{BOILERSUIT}` -> " + ", ".join(f"`{i.code}` ({i.customer_description[:44]})" for i in suits),
        ],
        expect=[],
    )
    return folder


def _case_3(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """Products that differ by one token in the middle of one sentence."""
    sneakers = [i for i in shelf if "sneaker" in i.customer_description.lower()]
    bolts = [i for i in shelf if "hexagon head bolt" in i.customer_description.lower()]
    assert len(sneakers) >= 3 and len(bolts) >= 4

    lines = [
        Line("", "steel toe safety sneakers, size 27", "4", "prs"),
        Line("", "steel toe safety sneakers, size 25", "2", "prs"),
        Line("", "hexagon head bolts full threaded with nut, M12 x 50", "300", "set"),
        Line("", "hexagon head bolts full threaded with nut, M8 x 50", "300", "set"),
    ]
    folder = _folder("3_sizes_that_differ_by_one_token")

    _email(
        folder,
        subject="Quotation request - MV HARBOUR PRIDE - Khalifa Port",
        sender="chief.officer@harbourpride.example.com",
        body=(
            "Dear Sirs,\n\n"
            "Please quote the below for MV HARBOUR PRIDE, IMO 9611234,\n"
            "delivery Khalifa Port, ETA 27 Oct. We have no item codes for these.\n\n"
            "1. steel toe safety sneakers, size 27 - 4 prs\n"
            "2. steel toe safety sneakers, size 25 - 2 prs\n"
            "3. hexagon head bolts full threaded with nut, M12 x 50 - 300 set\n"
            "4. hexagon head bolts full threaded with nut, M8 x 50 - 300 set\n\n"
            "Chief Officer"
        ),
    )
    (folder / "items.csv").write_bytes(_csv(lines))

    _expected(
        folder,
        what=(
            "Four lines with no codes at all, against products whose descriptions are identical "
            "except for one size token. Every line goes down the search branch, and the only "
            "thing that can order the shortlist correctly is that `27` and `M12` are rare words "
            "and `sneakers` and `bolt` are not."
        ),
        checks=[
            "all four lines: `how` = `search`, `item_code` empty, five candidates each",
            "line 1: the size 27 sneaker is FIRST in the shortlist, not 25, 26 or 29",
            "line 2: the size 25 sneaker is first",
            "line 3: the M12 x 50 bolt is first",
            "line 4: the M8 x 50 bolt is first",
            "the first candidate always shows 100% - read the ORDER, not the number",
            "a wrong size in first place is the failure this case exists to catch",
        ],
        lines=lines,
        notes=[
            "on the shelf: " + ", ".join(f"`{i.code}` {i.customer_description[:34]}" for i in sneakers[:4]),
        ],
        expect=[(None, catalog.search(one.description, limit=3)) for one in lines],
    )
    return folder


def _case_4(shelf: list[CatalogItem], _: Catalog) -> Path:
    """A customer email that is emphatically not an RFQ."""
    item = _pick(shelf, "M16*65")
    assert item

    folder = _folder("4_purchase_order_not_rfq")

    _email(
        folder,
        subject="PO 4500219871 - MV EASTERN DAWN - confirmation of order",
        sender="purchasing@easterndawn.example.com",
        body=(
            "Dear Supply team,\n\n"
            "Thank you for your quotation QOT-11294. We confirm our order as per\n"
            "the attached purchase order PO 4500219871, total USD 4,812.00.\n\n"
            "Delivery Fujairah anchorage 04 Nov as agreed. Please acknowledge\n"
            "receipt and confirm the delivery date by return.\n\n"
            "This is an order, not an enquiry - no quotation is required.\n\n"
            "Regards,\nPurchasing"
        ),
    )
    (folder / "PO 4500219871.pdf").write_bytes(
        pdf_with_text(
            [
                "PURCHASE ORDER 4500219871 - MV EASTERN DAWN",
                "Against your quotation QOT-11294.  Delivery Fujairah 04 Nov.",
                f"1. {item.customer_description[:58]}  -  250 set  @ USD 0.42",
                "2. WELDING GOGGLES  -  20 pcs  @ USD 6.10",
                "TOTAL USD 4,812.00",
            ]
        )
    )

    _expected(
        folder,
        what=(
            "A purchase order from a customer we already quoted. It carries a vessel, a port, a "
            "line list and prices, so everything that makes an RFQ look like an RFQ is present - "
            "and it is an order, which is a different piece of work for a different team."
        ),
        checks=[
            "category `CUSTOMER_ORDER_PO`, NOT `NEW_RFQ`",
            "`is_rfq` false -> it must not appear in GET /api/v1/quotes",
            "no KASS form is filled and nothing is forwarded to a desk",
            "`matching` is empty - matching runs for RFQs, and this is not one",
            "the record still exists with the verdict and the reasoning: this is not a dropped email",
            "the line list in the PDF must not talk the agent into treating it as a requisition",
        ],
        lines=[],
        notes=[],
        expect=[],
    )
    return folder


def _case_5(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """Sixty lines, to see that size changes nothing but the time."""
    chosen = shelf[:60]
    lines = [
        Line(item.customer_code, item.customer_description, str(1 + index % 12), "pcs")
        for index, item in enumerate(chosen)
    ]
    folder = _folder("5_long_requisition")

    _email(
        folder,
        subject="RFQ 77120 / MV GRAND MERIDIAN / Jebel Ali / annual stores",
        sender="procurement@grandmeridian.example.com",
        body=(
            "Dear Sirs,\n\n"
            "Annual stores replenishment for MV GRAND MERIDIAN, IMO 9233571,\n"
            "delivery Jebel Ali, ETA 15 Dec. Full list attached, 60 lines.\n"
            "Quotation required by 05 Dec. Currency USD.\n\n"
            "Regards,\nProcurement"
        ),
    )
    (folder / "Annual stores.xlsx").write_bytes(_xlsx(lines, header=True))

    _expected(
        folder,
        what=(
            "Sixty lines in one requisition. Nothing here is hard to match - the point is that a "
            "long RFQ costs the same as a short one and comes back in the order it arrived."
        ),
        checks=[
            f"{len(lines)} lines read, and the grid on the form holds all {len(lines)} in the original order",
            "ONE model call for the whole RFQ - restating the lines is batched, and nothing else asks the model",
            "the log line reads `Matched | N of 60 line(s) confirmed by code`",
            "no line is silently dropped: the record has exactly 60 entries under `matching`",
            "currency USD is read off the body, so the form has no starred cell left blank for it",
            "every line quotes a code the sheet has, so most should be `code_confirmed`",
        ],
        lines=lines[:8],
        notes=[
            f"Only the first 8 of {len(lines)} lines are listed above - the rest are more of the same.",
            "Products are taken in sheet order, so this also sweeps the catalogue rather than one corner of it.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines[:8]],
    )
    return folder


def _case_6(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """Numbers and units written the way people actually write them."""
    bolt = _pick(shelf, "M16*65")
    goggles = _pick(shelf, "WELDING GOGGLES")
    ruler = _pick(shelf, "Convex rulers")
    assert bolt and goggles and ruler

    lines = [
        # A decimal comma. It must survive as text, not become 15 or 1.5.
        Line(bolt.customer_code, bolt.customer_description, "1,5", "box"),
        # A quantity with its unit inside it, and an empty UOM column.
        Line(goggles.customer_code, goggles.customer_description, "2 coil", ""),
        # Leading zeros on a code that is not in the sheet at all.
        Line("0000512", "Convex rulers steel 5 metre", "6", "PCS"),
        # The same line twice, same quantity. Not a revision - a duplicate.
        Line(ruler.customer_code, ruler.customer_description, "3", "pcs"),
        Line(ruler.customer_code, ruler.customer_description, "3", "pcs"),
        # No quantity at all.
        Line(bolt.customer_code, bolt.customer_description, "", "set"),
    ]
    folder = _folder("6_messy_numbers_and_units")

    _email(
        folder,
        subject="stores req - MV NORTH CAPE - Sharjah - pls quote",
        sender="master@northcape.example.com",
        body=(
            "hello\n\n"
            "req attached for MV NORTH CAPE imo 9188512, sharjah, eta 09 nov.\n"
            "quantities are as our storekeeper wrote them, pls check anything odd.\n\n"
            "master"
        ),
    )
    (folder / "req.xlsx").write_bytes(_xlsx(lines, header=False))

    _expected(
        folder,
        what=(
            "Quantities and units exactly as a storekeeper types them: a decimal comma, a unit "
            "inside the quantity, a blank unit, a code padded with zeros, the same line twice and "
            "a line with no quantity at all. None of it may be quietly cleaned up."
        ),
        checks=[
            "line 1: quantity stays `1,5` in the record - not `15`, not `1.5`",
            "line 2: quantity stays `2 coil` and is NOT split into a number and a unit",
            "line 3: the code keeps its leading zeros - `0000512`, never `512`",
            "line 3's code is not in the sheet, so the line goes down the search branch",
            "lines 4 and 5 are identical: BOTH stay in the grid, and neither is collapsed into the other",
            "line 6 has no quantity: the cell is blank and the form says why, rather than guessing 1",
            "on the form, a quantity that is not a plain number is written as TEXT, so Excel cannot reinterpret it",
        ],
        lines=lines,
        notes=[
            "Decision 16 of the project: what looks like a number goes in as a number, except a "
            "code padded with zeros and a decimal comma. Both exceptions are on this page.",
            "Decision 22: two rows that say the same thing are two rows. Only the text of the "
            "email says which revision is current, and this email says nothing.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines if one.code],
    )
    return folder


def _case_7(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """Filenames as an attack, and one of them holds the requisition."""
    picks = [i for i in shelf[:3]]
    lines = [
        Line(item.customer_code, item.customer_description, qty, "pcs")
        for item, qty in zip(picks, ["8", "16", "24"], strict=False)
    ]
    folder = _folder("7_hostile_filenames")

    _email(
        folder,
        subject="RFQ - MV CASTELLAN - Port Rashid - see attachments",
        sender="ops@castellan.example.com",
        body=(
            "Dear Supply,\n\n"
            "Please quote for MV CASTELLAN, IMO 9401234, delivery Port Rashid,\n"
            "ETA 21 Nov. Our export tool names files badly - sorry. The real\n"
            "requisition is the spreadsheet.\n\n"
            "Operations"
        ),
    )
    # A dot-file. The record store strips leading dots, so this may not land as
    # a hidden file next to the record.
    (folder / ".env").write_bytes(b"SECRET_KEY=not-a-real-key\nNOTE=this is an attachment, not config\n")
    # The record store's own filenames. Neither may be overwritten by a file a
    # stranger named.
    (folder / "body.txt").write_bytes(b"This attachment is called body.txt on purpose.\n")
    # Unicode, punctuation and a name longer than the 120 characters the
    # record store keeps - but still short enough for a filesystem to hold, so
    # the case can exist as a folder at all.
    (folder / "Заявка на постачання ☠.xlsx").write_bytes(_xlsx(lines, header=True))
    (folder / f"{'requisition-' * 15}list.csv").write_bytes(_csv(lines))

    _expected(
        folder,
        what=(
            "Every attachment is named to cause trouble somewhere: a dot-file, two of the record "
            "store's own reserved names, a name in another script with an emoji, and a name long "
            "enough to break a path. One of them is the real requisition."
        ),
        checks=[
            "`.env` does not land in the record folder as a hidden file - the leading dot is stripped",
            "`body.txt` as an attachment does NOT overwrite the record's own `body.txt` - it lands beside it",
            "the unicode name survives readably, and the spreadsheet inside it is read",
            "the 184-character name is trimmed to 120 rather than refused, and `saved_as` says what it became",
            "nothing escapes the record's own folder: every `saved_as` is a plain filename",
            f"{len(lines)} lines are read out of the spreadsheet, and the csv holding the SAME lines "
            "does not double them",
            "verdict is still NEW_RFQ - a badly named file is not a reason to drop an email",
        ],
        lines=lines,
        notes=[
            "`_safe_name` in `src/infrastructure/storage/records.py` is what this case is aimed at. "
            "A filename is untrusted input that becomes a path.",
            "`.env` here is an attachment with harmless contents, not the project's own file.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines],
    )
    return folder


# --- writing a case out ---------------------------------------------------


def _folder(name: str) -> Path:
    folder = HERE / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    return folder


def _email(folder: Path, *, subject: str, sender: str, body: str) -> None:
    """The email twice: as a person would read it, and as the API takes it."""
    (folder / "email.txt").write_text(
        f"From: {sender}\nTo: {MAILBOX}\nSubject: {subject}\n\n{body}\n",
        encoding="utf-8",
    )
    (folder / "email.json").write_text(
        json.dumps(
            {
                "subject": subject,
                "sender": {"address": sender},
                "mailbox": MAILBOX,
                "body_text": body,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _expected(
    folder: Path,
    *,
    what: str,
    checks: list[str],
    lines: list[Line],
    notes: list[str],
    expect: list,
) -> None:
    """What the right answer is. Without this the folder is only a pile of files."""
    text = [f"# {folder.name}", "", what, "", "## What to check", ""]
    text += [f"- [ ] {check}" for check in checks]

    if lines:
        text += [
            "",
            "## The lines as the customer wrote them",
            "",
            "| # | Code | Description | Qty | UOM |",
            "|---|---|---|---|---|",
        ]
        text += [
            f"| {n} | `{one.code or '-'}` | {one.description} | {one.quantity or '-'} | {one.uom or '-'} |"
            for n, one in enumerate(lines, 1)
        ]

    if expect:
        text += ["", "## What the catalogue says about those codes and words", ""]
        for code, found in expect:
            if isinstance(found, list):
                names = ", ".join(f"`{c.item.code}`" for c in found) or "nothing"
                text.append(f"- words -> {names}")
            elif found is not None:
                text.append(f"- `{code}` -> `{found.code}` {found.description}")
            else:
                text.append(f"- `{code}` -> nothing in the sheet")

    if notes:
        text += ["", "## Notes", ""]
        text += [f"- {note}" for note in notes]

    text += [
        "",
        "## How to run it",
        "",
        "```bash",
        f"uv run python test_cases_2/run.py {folder.name.split('_')[0]}",
        "```",
        "",
        "The run writes a record under `Database/`, which is what the RFQ screens read.",
        "",
    ]
    (folder / "EXPECTED.md").write_text("\n".join(text), encoding="utf-8")


# --- the attachments ------------------------------------------------------


def _rows(lines: list[Line], header: bool) -> list[list]:
    rows: list[list] = []
    if header:
        rows += [
            ["REQUEST FOR QUOTATION", None, None, None, None],
            [None, None, None, None, None],
        ]
    rows.append(["Sr", "Item code", "Description", "Qty", "UOM"])
    rows += [[n, one.code, one.description, one.quantity, one.uom] for n, one in enumerate(lines, 1)]
    return rows


def _xlsx(lines: list[Line], *, header: bool = False) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Requisition"
    for row in _rows(lines, header):
        sheet.append(row)
    # Codes and quantities are text. A sheet that stores 0000512 as a number
    # loses the zeros and "1,5" becomes 15, and the agent has to survive both.
    for column in ("B", "D"):
        for cell in sheet[column]:
            cell.number_format = "@"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _csv(lines: list[Line]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    for row in _rows(lines, header=False):
        writer.writerow(["" if cell is None else cell for cell in row])
    return buffer.getvalue().encode("utf-8")


if __name__ == "__main__":
    sys.exit(main())
