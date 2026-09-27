"""Turn the desk's exported `Quote.xlsm` into the blank the quotation fills.

    uv run python -m src.tools.make_quote_template docs/samples/Quote.xlsm

The export is a real customer's, so it lives in `docs/samples/` - out of git
and out of the image - and only the blank this makes is shipped.

The desk has no empty copy of this workbook - every one it keeps was exported
for some customer. So the blank is made by clearing a filled one, and made by a
tool so that the next time SCINT changes its export, the blank is one command
away.

Cleared: every cell `quote_workbook` writes, the line row, and the customer
text the export left behind in the shared-string table - the cells are
rewritten as inline strings and the table emptied, so no previous customer's
name travels inside the template. The line sheet is renamed to a placeholder,
and the names of whoever last saved the file are taken out of its properties.

Kept byte for byte: the VBA project and its `Print` button, the logo, the
styles, the protection, the printer settings.
"""

import argparse
import io
import re
import zipfile
from pathlib import Path

from src.services.quote_workbook import (
    CORE,
    DETAILS,
    SHARED,
    SUMMARY,
    WORKBOOK,
    FALLBACK_SHEET,
    Office,
    QuoteBook,
    fill,
)

_SI = re.compile(r"<si>(.*?)</si>", re.DOTALL)
_T = re.compile(r"<t\b[^>]*>(.*?)</t>", re.DOTALL)
_SHARED_CELL = re.compile(
    r'<c r="(?P<ref>[A-Z]+\d+)"(?P<attrs>[^>]*?)\st="s"(?P<rest>[^>]*)>(?P<body>.*?)</c>', re.DOTALL
)
_EMPTY_SST = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    ' count="0" uniqueCount="0"/>'
)
_ABS_PATH = re.compile(r"<mc:AlternateContent\b.*?</mc:AlternateContent>", re.DOTALL)
_WHO = re.compile(r"<(dc:creator|cp:lastModifiedBy)>[^<]*</\1>")

# Two formulas the export stores in a form Excel itself never writes: `=H4`
# with its own leading `=` (so `==H4`), and a range with a space inside it,
# which in a formula is the intersection operator. Desktop Excel offers to
# repair such a file; Excel for the web refuses to open it. Both are put the
# way Excel would have saved them.
_REPAIRS = (
    ("<f ca=\"1\">=H4</f>", "<f ca=\"1\">H4</f>"),
    ("SUM(J20: J22)", "SUM(J20:J22)"),
)

BLANK = QuoteBook(
    office=Office(location="", address="", phone="", email="", tax_reg=""),
    number=FALLBACK_SHEET,
    vessel="",
    port="",
    client="",
    quoted_on=None,
)


def _strings(sst: str) -> list[str]:
    """The shared-string table, as plain text, rich runs joined."""
    return ["".join(_T.findall(item)) for item in _SI.findall(sst)]


def _inlined(sheet: str, strings: list[str]) -> str:
    """Every shared-string cell rewritten to carry its own text."""

    def one(match: re.Match[str]) -> str:
        body = match.group("body")
        attrs = match.group("attrs") + match.group("rest")
        value = re.search(r"<v>(\d+)</v>", body)
        text = strings[int(value.group(1))] if value else ""
        formula = re.search(r"<f\b[^>]*>.*?</f>", body, re.DOTALL)
        if formula:
            # A formula whose cached result was a shared string.
            return f'<c r="{match.group("ref")}"{attrs} t="str">{formula.group(0)}<v>{text}</v></c>'
        return (
            f'<c r="{match.group("ref")}"{attrs} t="inlineStr">'
            f'<is><t xml:space="preserve">{text}</t></is></c>'
        )

    return _SHARED_CELL.sub(one, sheet)


def build(source: Path, out: Path) -> None:
    with zipfile.ZipFile(source) as master:
        strings = _strings(master.read(SHARED).decode("utf-8"))
        inlined = io.BytesIO()
        with zipfile.ZipFile(inlined, "w", zipfile.ZIP_DEFLATED) as copy:
            for entry in master.infolist():
                data = master.read(entry.filename)
                if entry.filename in (SUMMARY, DETAILS):
                    sheet = _inlined(data.decode("utf-8"), strings)
                    for broken, repaired in _REPAIRS:
                        sheet = sheet.replace(broken, repaired)
                    data = sheet.encode("utf-8")
                elif entry.filename == SHARED:
                    data = _EMPTY_SST.encode("utf-8")
                elif entry.filename == CORE:
                    data = _WHO.sub(r"<\1></\1>", data.decode("utf-8-sig")).encode("utf-8")
                elif entry.filename == WORKBOOK:
                    # The desk's own disk path, `C:\Workspace\...`, has no place
                    # in a file sent to customers.
                    data = _ABS_PATH.sub("", data.decode("utf-8")).encode("utf-8")
                copy.writestr(entry, data)

    # Clearing is filling with nothing: the same code that writes a quotation
    # writes the blank, so the two cannot disagree about which cells are ours.
    blank = fill(inlined.getvalue(), BLANK)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blank)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="a Quote.xlsm SCINT exported")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("config/quotation_sg_uae_workbook_template.xlsm"),
        help="where to write the blank",
    )
    args = parser.parse_args()
    build(args.source, args.out)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

