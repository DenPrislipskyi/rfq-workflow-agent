"""Build five RFQ test cases out of the real product catalogue.

    uv run python test_cases_3/build.py

`test_cases/` covers the shape of the pipeline and `test_cases_2/` covers the
matching. This set is aimed at the three things that were just fixed, plus two
routing shapes neither of the others has:

    1  words the sheet already knows   the agreement is measured on the
                                       customer's own sentence, not only ours
    2  port only in the attachment     routing reads the full header, not the
                                       verdict's guess at it
    3  quantities that are missing     a line nobody counted is still a line,
                                       and a section heading still is not one
    4  two ports, one email            two desks match at once - guess neither
    5  a revision in a reply           route on the newest message, not on the
                                       port quoted below it

Each folder is one email as a person would have sent it, the files that came
with it, and `EXPECTED.md` saying what should happen and why.

Run `test_cases_3/run.py <n>` to push one case through the real pipeline.
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
from src.domain.rules.catalog import Catalog, CatalogItem, tokenize  # noqa: E402
from src.infrastructure.catalog import Snapshot  # noqa: E402

HERE = Path(__file__).parent
MAILBOX = "supply@sevenseas.example.com"

# The two rows the desk put in the sheet as examples of a mapping that went
# wrong - the code names one product and the customer wanted another. They are
# `test_cases_2/7`'s subject; here they would only be noise in a case about
# something else.
BAD_MAPPINGS = {"110188", "470285"}


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
    if len(shelf) < 8:
        print(f"Only {len(shelf)} usable products in the catalogue - need at least 8.")
        return 1

    for build in (_case_1, _case_2, _case_3, _case_4, _case_5):
        folder = build(shelf, catalog)
        files = sorted(p.name for p in folder.iterdir())
        print(f"{folder.name:32} {len(files)} file(s): {', '.join(files)}")

    print(f"\nFive cases under {HERE}. Run one with: uv run python test_cases_3/run.py 1")
    return 0


def _shelf(catalog: Catalog) -> list[CatalogItem]:
    """Products whose own recorded wording finds them, placeholders excluded."""
    found = []
    for item in catalog.items:
        if not (item.customer_code and item.customer_description):
            continue
        if item.customer_description.lower().startswith("not "):
            continue
        if item.customer_code.lower().startswith("not "):
            continue
        if item.customer_code in BAD_MAPPINGS:
            continue
        best = catalog.search(item.customer_description, limit=1)
        if best and best[0].item.code == item.code:
            found.append(item)
    return found


def _pick(shelf: list[CatalogItem], needle: str) -> CatalogItem:
    found = next((i for i in shelf if needle.lower() in i.customer_description.lower()), None)
    assert found is not None, f"nothing on the shelf matching {needle!r}"
    return found


def _agreement(ours: str, theirs: str) -> int:
    """The pipeline's own measure, so `EXPECTED.md` can state a real number."""
    mine, yours = set(tokenize(ours)), set(tokenize(theirs))
    return round(100 * len(mine & yours) / len(mine)) if mine else 0


# --- the cases ------------------------------------------------------------


def _case_1(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """Lines written in the sheet's own words, beside lines that are not."""
    ruler = _pick(shelf, "Convex rulers")
    bolt16 = _pick(shelf, "M16*65")
    goggles = _pick(shelf, "WELDING GOGGLES")
    gloves = _pick(shelf, "five fingers")

    lines = [
        # Word for word what the sheet files this product under. These are the
        # lines that were being refused: our restatement of them said the same
        # thing in our own words, and only that was compared.
        Line(ruler.customer_code, ruler.customer_description, "6", "pcs"),
        Line(bolt16.customer_code, bolt16.customer_description, "400", "set"),
        Line(goggles.customer_code, goggles.customer_description, "12", "pcs"),
        # The same code, a different product. Must still lose.
        Line(bolt16.customer_code, "Hexagon head bolts with nut M20 x 80, full thread", "50", "set"),
        # A code the sheet has, and words for something else entirely.
        Line(gloves.customer_code, "Fire hose 2.5 inch, 20 metre, with couplings", "4", "pcs"),
        # No code at all, words the sheet knows.
        Line("", goggles.customer_description, "5", "pcs"),
    ]
    folder = _folder("1_words_the_sheet_already_knows")

    _email(
        folder,
        subject="RFQ 88210 / MV WESTERN STAR / Jebel Ali",
        sender="purchasing@westernstar.example.com",
        body=(
            "Dear Sirs,\n\n"
            "Please quote the attached for MV WESTERN STAR, IMO 9256379,\n"
            "delivery Jebel Ali, ETA 08 Dec. Quotation required by 01 Dec.\n"
            "Currency USD.\n\n"
            "Most of these are copied straight out of your own catalogue.\n\n"
            "Regards,\nPurchasing"
        ),
    )
    (folder / "Requisition.xlsx").write_bytes(_xlsx(lines, header=True))

    _expected(
        folder,
        what=(
            "Three lines written exactly as the sheet files those products, and three that are "
            "not. Until the comparison was fixed, a line that matched the sheet word for word "
            "had its code dropped, because only our restatement of it was compared - "
            "`RULE CONVEX` against `Convex rulers` scored 50%."
        ),
        checks=[
            "lines 1, 2, 3: `code_confirmed`, confidence 100, NO candidates - this is the fix",
            "line 4: same code as line 2, but an M20 is not an M16 -> `code_rejected` with a top-5",
            "line 4's shortlist puts the M20 x 80 bolt first",
            "line 5: the code names gloves and the words say fire hose -> `code_rejected`",
            "line 6: no code, words the sheet knows -> `search`, and the goggles first in the list",
            "if any of lines 1-3 comes back `code_rejected`, the fix did not hold",
        ],
        lines=lines,
        notes=[
            f"line 1 against `{ruler.customer_code}`: **{_agreement(lines[0].description, ruler.customer_description)}%** word for word",
            f"line 4 against `{bolt16.customer_code}`: **{_agreement(lines[3].description, bolt16.customer_description)}%**",
            f"line 5 against `{gloves.customer_code}`: **{_agreement(lines[4].description, gloves.customer_description)}%**",
            "The model restates each line before this is measured, and the better of the two "
            "comparisons stands - so these numbers are the floor.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines if one.code],
    )
    return folder


def _case_2(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """The port is in the file and nowhere else."""
    lines = [
        Line(item.customer_code, item.customer_description, qty, "pcs")
        for item, qty in zip(shelf[:4], ["10", "20", "30", "40"], strict=False)
    ]
    folder = _folder("2_port_only_in_the_attachment")

    _email(
        folder,
        subject="Requisition attached - MV IRON DUKE - please quote",
        sender="purchasing@irondukeship.example.com",
        body=(
            "Dear Supply team,\n\n"
            "Kindly quote the attached requisition. All delivery details are on\n"
            "the form itself.\n\n"
            "Thank you,\nPurchasing Department"
        ),
    )
    (folder / "Requisition.xlsx").write_bytes(
        _xlsx(
            lines,
            preamble=[
                ["REQUEST FOR QUOTATION", None, None, None, None],
                ["Vessel:", "MV IRON DUKE", None, None, None],
                ["IMO:", "9284108", None, None, None],
                ["Delivery:", "Port Rashid", None, None, None],
                ["ETA:", "19 Dec", None, None, None],
                [None, None, None, None, None],
            ],
        )
    )

    _expected(
        folder,
        what=(
            "Neither the subject nor the body names a port. `Port Rashid` is in the attachment, "
            "in the rows above the table. Routing used to ask the verdict pass for the port - it "
            "is asked for a verdict and answers about the port in passing - and an email like "
            "this one went nowhere."
        ),
        checks=[
            "the port `Port Rashid` is read off the attachment and appears on the form",
            "the RFQ is FORWARDED, to the UAE desk - Port Rashid is a Dubai port",
            "`delivery.outcome` is `SENT`, and the label is `SSG RFQ` WITHOUT `SSG Not Sent`",
            "the form's SUPPLIER NAME reads UAE-DUBAI - the branch and the address agree",
            "vessel MV IRON DUKE and IMO 9284108 also come off the attachment, not the body",
            f"{len(lines)} lines, all quoting codes the sheet has",
        ],
        lines=lines,
        notes=[
            "`port rashid` was in the region's port list but not in its keyword list, so even "
            "the fallback could not catch it. Both were fixed.",
            "A second thing this checks: the header reader only sees rows ABOVE the table, "
            "which is where these are.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines],
    )
    return folder


def _case_3(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """Rows that are items without being counted, and rows that are not items."""
    bolt = _pick(shelf, "M16*65")
    goggles = _pick(shelf, "WELDING GOGGLES")
    gloves = _pick(shelf, "five fingers")
    ruler = _pick(shelf, "Convex rulers")

    lines = [
        Line(bolt.customer_code, bolt.customer_description, "100", "set"),
        # Code and unit, no quantity. Somebody meant to order this and forgot
        # to count it. It must not vanish.
        Line(goggles.customer_code, goggles.customer_description, "", "pcs"),
        # Unit only, no code, no quantity. Still a line.
        Line("", gloves.customer_description, "", "prs"),
        Line(ruler.customer_code, ruler.customer_description, "6", "pcs"),
    ]
    folder = _folder("3_quantities_that_are_missing")

    # Headings and a spacer, interleaved with the real lines. They look like
    # rows and are not: nobody is ordering a quantity of "DECK STORES".
    rows = [
        ["REQUEST FOR QUOTATION", None, None, None, None],
        [None, None, None, None, None],
        ["Sr", "Item code", "Description", "Qty", "UOM"],
        [None, None, "FASTENERS [69]", None, None],
        [1, lines[0].code, lines[0].description, lines[0].quantity, lines[0].uom],
        [None, None, "SAFETY [85]", None, None],
        [2, lines[1].code, lines[1].description, lines[1].quantity, lines[1].uom],
        [3, lines[2].code, lines[2].description, lines[2].quantity, lines[2].uom],
        [None, None, None, None, None],
        [None, None, "MEASURING [65]", None, None],
        [4, lines[3].code, lines[3].description, lines[3].quantity, lines[3].uom],
    ]

    _email(
        folder,
        subject="RFQ - MV SOUTHERN CROSS - Khor Fakkan - deck and safety",
        sender="chief.officer@southerncross.example.com",
        body=(
            "Good day,\n\n"
            "Please quote attached for MV SOUTHERN CROSS, IMO 9312371,\n"
            "delivery Khor Fakkan, ETA 12 Dec.\n\n"
            "Two of the lines have no quantity yet - the Master will confirm\n"
            "them before the order. Please price them anyway.\n\n"
            "Chief Officer"
        ),
    )
    (folder / "Requisition.xlsx").write_bytes(_sheet(rows))

    _expected(
        folder,
        what=(
            "A requisition with catalogue headings written into the description column, a spacer "
            "row, and two lines nobody put a quantity against. The headings must be dropped and "
            "the uncounted lines must not - a position that disappears is a position the customer "
            "never learns was not quoted."
        ),
        checks=[
            f"exactly {len(lines)} lines are read - the three headings and the spacer are not items",
            "`FASTENERS [69]`, `SAFETY [85]` and `MEASURING [65]` appear nowhere in the record",
            "line 2 is present with an EMPTY quantity - not 1, not dropped",
            "line 3 is present with an empty quantity and no code, carrying only its unit",
            "the form's grid holds all four, and the quantity cells of 2 and 3 are blank",
            "the body says two lines have no quantity - the count in the record must agree with it",
        ],
        lines=lines,
        notes=[
            "The rule that drops headings is deliberate and stays: a heading is a description and "
            "nothing else. What changed is that a row carrying a code or a unit is no longer "
            "treated as one.",
            "Worth watching even though it is not a check: whether the model maps the columns at "
            "all when a third of the rows are headings.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines if one.code],
    )
    return folder


def _case_4(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """Two desks answer to this email. Guessing is worse than not sending."""
    lines = [
        Line(item.customer_code, item.customer_description, qty, "pcs")
        for item, qty in zip(shelf[:3], ["5", "10", "15"], strict=False)
    ]
    folder = _folder("4_two_ports_one_email")

    _email(
        folder,
        subject="Quotation request - MV PACIFIC TRADER - stores",
        sender="ops@pacifictrader.example.com",
        body=(
            "Dear Sirs,\n\n"
            "Please quote the attached for MV PACIFIC TRADER, IMO 9339159.\n\n"
            "The vessel is alongside in Singapore until Friday and then sails\n"
            "for Dubai. We will confirm the delivery place once the charterers\n"
            "decide - please quote for both so we can compare.\n\n"
            "Operations"
        ),
    )
    (folder / "Stores list.csv").write_bytes(_csv(lines))

    _expected(
        folder,
        what=(
            "The body names Singapore and Dubai, and says plainly that the delivery place is not "
            "settled. Two desks answer to it, which is the one case where the region rules stop "
            "rather than fall through to a weaker signal: a misrouted RFQ is lost in silence."
        ),
        checks=[
            "the email IS an RFQ - `NEW_RFQ`, and the form is filled",
            "it is NOT forwarded: `delivery.outcome` is `NO_REGION`",
            "labels are `SSG RFQ` AND `SSG Not Sent` - it is an RFQ that a person has to route",
            "the form's DELIVERY PORT is blank or carries whatever the customer wrote, not a guess",
            "SUPPLIER NAME (the branch) is blank - no branch was chosen",
            f"the {len(lines)} lines are still read and matched: routing failed, reading did not",
            "sending this to one desk and not the other would be the failure, not the empty region",
        ],
        lines=lines,
        notes=[
            "`resolve_region` returns None as soon as one rule names two regions. It does not fall "
            "through to the next rule - a genuine conflict must not be settled by a weaker signal.",
            "This is the intended answer, not a limitation. The email says the place is undecided.",
        ],
        expect=[(one.code, catalog.by_code(one.code)) for one in lines],
    )
    return folder


def _case_5(shelf: list[CatalogItem], catalog: Catalog) -> Path:
    """A revision on top of a thread whose older message names another port."""
    lines = [
        Line(item.customer_code, item.customer_description, qty, "pcs")
        for item, qty in zip(shelf[:3], ["24", "12", "6"], strict=False)
    ]
    folder = _folder("5_a_revision_in_a_reply")

    _email(
        folder,
        subject="RE: RFQ 90551 / MV BLUE HORIZON / revised delivery",
        sender="purchasing@bluehorizon.example.com",
        body=(
            "Dear Supply,\n\n"
            "Further to the below - the vessel's schedule has changed. Delivery\n"
            "is now FUJAIRAH, ETA 22 Dec. Please disregard the Singapore call.\n"
            "The item list is unchanged and attached again for convenience.\n\n"
            "Regards,\nPurchasing\n\n"
            "-----Original Message-----\n"
            "From: purchasing@bluehorizon.example.com\n"
            "Sent: Monday\n"
            "Subject: RFQ 90551 / MV BLUE HORIZON / Singapore\n\n"
            "Dear Sirs,\n\n"
            "Please quote the attached for MV BLUE HORIZON, IMO 9418727,\n"
            "delivery Singapore, ETA 14 Dec. Quotation required by 08 Dec.\n\n"
            "Regards,\nPurchasing"
        ),
    )
    (folder / "Item list.xlsx").write_bytes(_xlsx(lines, header=True))

    _expected(
        folder,
        what=(
            "A reply that moves the delivery from Singapore to Fujairah, with the original message "
            "quoted underneath still saying Singapore. The port that counts is the one in the "
            "newest message; the quoted history is context, not an instruction."
        ),
        checks=[
            "the RFQ goes to the **UAE** desk (Fujairah), NOT to Singapore",
            "the form's DELIVERY PORT reads Fujairah and the ETA is 22 Dec, not 14 Dec",
            "the category is `UPDATED_RFQ` (a revision) - `NEW_RFQ` is acceptable, the routing is not",
            "the RFQ reference `RFQ 90551` is carried through from the subject",
            f"{len(lines)} lines are read once - the list is attached, not quoted twice",
            "routing to Singapore is the failure this case exists to catch",
        ],
        lines=lines,
        notes=[
            "Only the subject and the newest message go to the region rules. Quoted history does "
            "not: an older message about another port would route this one to the wrong desk.",
            "The quote deadline is in the OLD message only. Whether it is carried forward is "
            "worth looking at, but it is not a pass or fail here.",
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
            f"| {n} | `{one.code or '-'}` | {one.description} | {one.quantity or '**(none)**'} | {one.uom or '-'} |"
            for n, one in enumerate(lines, 1)
        ]

    if expect:
        text += ["", "## What the catalogue says about those codes", ""]
        for code, found in expect:
            if found is not None:
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
        f"uv run python test_cases_3/run.py {folder.name.split('_')[0]}",
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


def _sheet(rows: list[list]) -> bytes:
    """Any grid, written the way a real export writes one."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Requisition"
    for row in rows:
        sheet.append(row)
    # Codes and quantities are text: a sheet that stores 0012345 as a number
    # loses the zeros, and the agent has to survive both.
    for column in ("B", "D"):
        for cell in sheet[column]:
            cell.number_format = "@"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlsx(lines: list[Line], *, header: bool = False, preamble: list[list] | None = None) -> bytes:
    """The item grid, optionally under rows the header reader has to find."""
    return _sheet((preamble or []) + _rows(lines, header and not preamble))


def _csv(lines: list[Line]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    for row in _rows(lines, header=False):
        writer.writerow(["" if cell is None else cell for cell in row])
    return buffer.getvalue().encode("utf-8")


if __name__ == "__main__":
    sys.exit(main())
