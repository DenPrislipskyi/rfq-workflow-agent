"""A workbook shaped like the master, small enough to fill in a millisecond.

The real template is 13 MB on disk and 127 MB unpacked, and one test does use
it - fidelity to that exact file is the point of stage C. Everything else about
the writer is about shapes rather than size, and this is those shapes: a cell
that already carries a date format and one that does not, cells the master
ships demo values in, a formula that must survive, a row whose column C is
simply absent, and a binary part to prove it is copied and not rebuilt.
"""

import io
import zipfile

VBA = b"\x00\x01fake vba project\xff\xfe"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
<Relationship Id="rId4" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/>
</Relationships>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="KASS RFQ Template" sheetId="1" r:id="rId1"/></sheets><calcPr/></workbook>"""

# 0 is the default, 1 is plain text, 2 already shows a date, 3 shows a date and
# a time. `Quote Before` sits on 1, which is the whole reason `styles.py` exists.
STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="[$-409]d\\-mmm\\-yyyy"/></numFmts>
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="22" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

# The demo values the master ships with, which every copy has to overwrite.
STRINGS = ["SINGAPORE", "UAE - DUBAI", "REQUEST FOR QUOTE", "AED"]

SHARED_STRINGS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(STRINGS)}" uniqueCount="{len(STRINGS)}">'
    + "".join(f"<si><t>{value}</t></si>" for value in STRINGS)
    + "</sst>"
)

LAST_ROW = 40


def _row(number: int) -> str:
    """One row of the form, shaped the way the master shapes it."""
    cells = {
        1: '<c r="A1" s="1" t="str"><f t="array" ref="A1">IFERROR(INDEX(X2:X99,MATCH(H9,AD2:AD99,0)),"Enter the Sender Code")</f><v>Enter the Sender Code</v></c>',
        2: '<c r="C2" s="1"/><c r="H2" s="1" t="s"><v>1</v></c>',
        3: '<c r="C3" s="1"/><c r="H3" s="2"/>',
        4: '<c r="C4" s="1"/><c r="H4" s="2"/>',
        5: '<c r="C5" s="1" t="s"><v>0</v></c><c r="H5" s="1"/>',
        6: '<c r="C6" s="1"/><c r="H6" s="1"/>',
        7: '<c r="C7" s="1"/><c r="H7" s="1"/>',
        8: '<c r="C8" s="1"/><c r="H8" s="1" t="s"><v>3</v></c>',
        9: '<c r="C9" s="1"/><c r="H9" s="1"/>',
        10: '<c r="C10" s="1" t="s"><v>2</v></c><c r="H10" s="1" t="str"><f>TEXT(H12,"dd-MMM")</f><v>00-Jan</v></c>',
        11: '<c r="H11" s="1"/>',
        12: '<c r="H12" s="3"/>',
        23: "".join(
            f'<c r="{letter}23" s="1" t="inlineStr"><is><t>{name}</t></is></c>'
            for letter, name in zip("ABCDEF", ("SR", "CODE", "ITEM CODE", "DESCRIPTION", "QTY", "UOM"))
        ),
    }
    # Column C is missing from the item rows, exactly as it is in the master:
    # the writer has to insert a cell that is not there rather than assume one.
    body = cells.get(number, '<c r="A{0}" s="1"/><c r="B{0}" s="1"/><c r="D{0}" s="1"/><c r="E{0}" s="1"/><c r="F{0}" s="1"/>'.format(number))
    return f'<row r="{number}">{body}</row>'


SHEET = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<sheetData>" + "".join(_row(number) for number in range(1, LAST_ROW + 1)) + "</sheetData>"
    "</worksheet>"
)

PARTS = {
    "[Content_Types].xml": CONTENT_TYPES,
    "_rels/.rels": ROOT_RELS,
    "xl/workbook.xml": WORKBOOK,
    "xl/_rels/workbook.xml.rels": WORKBOOK_RELS,
    "xl/styles.xml": STYLES,
    "xl/sharedStrings.xml": SHARED_STRINGS,
    "xl/worksheets/sheet1.xml": SHEET,
    "xl/vbaProject.bin": VBA,
}


def master() -> bytes:
    """The whole thing, as a `.xlsm` file's bytes."""
    return _packed(PARTS)


def with_sheet(sheet: str) -> bytes:
    """The same workbook with a different worksheet, for the tests about rows
    the sheet does not have."""
    return _packed(PARTS | {"xl/worksheets/sheet1.xml": sheet})


def _packed(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as book:
        for name, content in parts.items():
            book.writestr(name, content)
    return buffer.getvalue()
