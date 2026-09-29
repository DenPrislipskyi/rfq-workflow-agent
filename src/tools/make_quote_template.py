"""Turn the desk's exported `Quote.xlsm` into the blank the quotation fills.

    uv run python -m src.tools.make_quote_template docs/samples/Quote.xlsm
    uv run python -m src.tools.make_quote_template --restyle \\
        config/quotation_sg_uae_workbook_template.xlsm

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

Every font is set to one face and one size - Arial 10 - keeping only bold,
italic, underline and colour. The export mixes Arial 8, 10, 11, 12 and 14 with
Calibri 11 and 12, and a quotation whose cells change size from one to the next
reads as a mistake. `--restyle` does only this, to a template already made:
the export it was made from need not be kept.

Kept byte for byte: the VBA project and its `Print` button, the logo, the
protection, the printer settings.
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
    STYLES,
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

FONT = "Arial"
SIZE = "10"

_FONTS = re.compile(r"<fonts\b[^>]*>.*?</fonts>", re.DOTALL)
_FONT = re.compile(r"<font>(.*?)</font>|<font\s*/>", re.DOTALL)
# What a font says about face and size - and `scheme`, which ties it to the
# theme's font and would let Excel put Calibri back.
_FACE = re.compile(r"<(?:sz|name|family|scheme)\b[^>]*/>")


def uniform_fonts(styles: str) -> str:
    """The style sheet with every cell font in one face and size.

    Only the `<fonts>` table is touched: bold, italic, underline and colour
    stay, and the conditional formats' own fonts - colour only - are left
    alone.
    """

    def one(match: re.Match[str]) -> str:
        kept = _FACE.sub("", match.group(1) or "")
        return f'<font>{kept}<sz val="{SIZE}" /><name val="{FONT}" /></font>'

    table = _FONTS.search(styles)
    if table is None:
        raise ValueError("the workbook has no font table")
    return styles[: table.start()] + _FONT.sub(one, table.group(0)) + styles[table.end() :]


def restyle(template: bytes) -> bytes:
    """The same workbook with `uniform_fonts` applied, every other part as it was."""
    with zipfile.ZipFile(io.BytesIO(template)) as master:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
            for entry in master.infolist():
                data = master.read(entry.filename)
                if entry.filename == STYLES:
                    data = uniform_fonts(data.decode("utf-8")).encode("utf-8")
                copy.writestr(entry, data)
    return buffer.getvalue()


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
    blank = restyle(fill(inlined.getvalue(), BLANK))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blank)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="a Quote.xlsm SCINT exported")
    parser.add_argument(
        "--restyle",
        action="store_true",
        help="only set one font and size in SOURCE, an already made template, in place",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("config/quotation_sg_uae_workbook_template.xlsm"),
        help="where to write the blank",
    )
    args = parser.parse_args()
    if args.restyle:
        args.source.write_bytes(restyle(args.source.read_bytes()))
        print(f"Restyled {args.source}")
        return
    build(args.source, args.out)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

