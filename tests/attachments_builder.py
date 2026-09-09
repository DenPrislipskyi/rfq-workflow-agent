"""Attachments built in code, so the test suite carries no binary blobs.

Each builder produces the smallest file that still exercises one real reader
path. The awkward one is the PDF: there is no permissively licensed writer in
the dependency set, so a minimal PDF is assembled by hand with a correct xref
table - readers reject a broken one, and a reader that silently repairs it
would be testing the repair rather than the parse.
"""

import csv
import io
import zipfile
from email.message import EmailMessage

import docx
import openpyxl
import pptx
from PIL import Image
from pptx.util import Inches

# The requisition every test reads back. Three items, an IMPA column, a UOM the
# workbook's own macro would normalize, and a header row that is not row one -
# real exports put a title above the table.
REQUISITION_ROWS = [
    ["REQUISITION 78432", None, None, None, None],
    [None, None, None, None, None],
    ["ITEM", "IMPA", "DESCRIPTION", "QTY", "UNIT"],
    [1, "550101", "ROPE PP 24MM X 220M", 2, "coil"],
    [2, "232101", "PAINT MARINE WHITE 20L", 5, "can"],
    [3, "311204", "GASKET SET, PUMP (see drawing)", 1, "set"],
]


def xlsx_requisition() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Requisition"
    for row in REQUISITION_ROWS:
        sheet.append(row)
    # Excel keeps the rows a user once scrolled through; the reader must drop them.
    sheet.cell(row=900, column=1, value=None)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def xlsx_of(rows: list[list], name: str = "Requisition") -> bytes:
    """Any sheet, for the tests that need the rows to differ."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = name
    for row in rows:
        sheet.append(row)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def csv_requisition(delimiter: str = ";") -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter)
    for row in REQUISITION_ROWS[2:]:
        writer.writerow(["" if cell is None else cell for cell in row])
    return buffer.getvalue().encode("utf-8")


def docx_with_table() -> bytes:
    document = docx.Document()
    document.add_paragraph("Dear Sirs, kindly quote for MV ALMI GLOBE.")
    table = document.add_table(rows=0, cols=5)
    for row in REQUISITION_ROWS[2:]:
        cells = table.add_row().cells
        for cell, value in zip(cells, row, strict=True):
            cell.text = "" if value is None else str(value)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def pptx_deck() -> bytes:
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "RFQ 78432 - MV ALMI GLOBE"

    rows, cols = len(REQUISITION_ROWS) - 2, 5
    table = slide.shapes.add_table(
        rows, cols, Inches(0.5), Inches(2), Inches(9), Inches(3)
    ).table
    for r, row in enumerate(REQUISITION_ROWS[2:]):
        for c, value in enumerate(row):
            table.cell(r, c).text = "" if value is None else str(value)

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def png_photo(size: tuple[int, int] = (2400, 1600)) -> bytes:
    """Deliberately larger than the long-edge cap, so downscaling is exercised.

    Noise rather than a flat fill: a solid-colour PNG of any dimensions
    compresses to a few hundred bytes, which would make a 2400x1600 "photo"
    look like a signature logo to the size test it is meant to defeat.
    """
    image = Image.effect_noise(size, 48).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def html_pretending_to_be_xls() -> bytes:
    """What portal and ERP exports actually send under an .xls name."""
    return (
        b"<html><head><title>Export</title></head><body>"
        b"<table><tr><td>ITEM</td><td>QTY</td></tr></table>"
        b"</body></html>"
    )


def zip_of(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # A resource fork the sender never meant to send; the reader skips it.
        archive.writestr("__MACOSX/._Requisition.xlsx", b"junk")
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def eml_with_attachment(payload: bytes, filename: str) -> bytes:
    message = EmailMessage()
    message["From"] = "purchasing@almiship.com"
    message["To"] = "rfq@example.com"
    message["Subject"] = "FW: RFQ 78432"
    message.set_content("Original request below, spreadsheet attached.")
    message.add_attachment(
        payload,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
    return message.as_bytes()


def pdf_with_text(lines: list[str] | None = None) -> bytes:
    """A one-page PDF carrying a real text layer.

    Long enough to clear `pdf.MIN_CHARS_PER_PAGE`: two short lines would be
    classified as a scan, which is the threshold working, not a bug.
    """
    lines = lines or [
        "REQUEST FOR QUOTATION 78432",
        "Vessel: MV ALMI GLOBE   IMO 9232395",
        "Delivery port: JEBEL ALI    ETA 12 Oct",
        "Please quote before 08 Oct. Items are listed in the attached sheet.",
    ]
    content = "BT /F1 14 Tf 40 700 Td 18 TL\n" + "\n".join(
        f"({_escape(line)}) Tj T*" for line in lines
    ) + "\nET"
    return _assemble_pdf(content.encode("ascii"))


def pdf_without_text() -> bytes:
    """A page with graphics and no text - what a scan looks like to a parser."""
    return _assemble_pdf(b"0.9 g 40 600 500 150 re f")


def pdf_of_pictures(caption: str = "Email Preview Attachment Preview") -> bytes:
    """A page that is one big image with a caption under it.

    What a presentation exported to PDF looks like: enough text to clear the
    no-text threshold, and all of the content in the picture.
    """
    content = (
        b"q 500 0 0 700 40 100 cm /Im0 Do Q\n"
        b"BT /F1 12 Tf 40 60 Td (" + _escape(caption).encode("ascii") + b") Tj ET"
    )
    image = (
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n"
        b"\x80\nendstream"
    )
    return _assemble_pdf(content, image=image)


def _assemble_pdf(stream: bytes, image: bytes | None = None) -> bytes:
    """Build a valid PDF, xref offsets included.

    Every object's byte offset has to be written into the xref table; readers
    that would otherwise reconstruct it are precisely what a fixture must not
    rely on.
    """
    xobject = b" /XObject << /Im0 6 0 R >>" if image else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >>" + xobject + b" >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if image:
        objects.append(image)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
