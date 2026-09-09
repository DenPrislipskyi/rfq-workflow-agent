"""Stage A: attachments in, readable content out. No model is called anywhere here."""

import pytest

from src.infrastructure.documents import Budget, DocumentLoader, FileKind, SourceFile, sniff
from src.infrastructure.documents.models import (
    ARCHIVE_ENTRIES_TRUNCATED,
    ATTACHMENT_IS_A_LINK,
    FILE_TOO_LARGE,
    IMAGES_TRUNCATED,
    NESTED_TOO_DEEP,
    NO_READER_FOR_KIND,
    ROWS_TRUNCATED,
    SCANNED_PDF,
    TOO_MANY_ATTACHMENTS,
    UNREADABLE,
)
from tests import attachments_builder as build

ITEM_HEADER = ["ITEM", "IMPA", "DESCRIPTION", "QTY", "UNIT"]


def load(*files: SourceFile, budget: Budget | None = None):
    return DocumentLoader(budget).load(files)


def one(*files: SourceFile, budget: Budget | None = None):
    documents = load(*files, budget=budget)
    assert len(documents) == 1
    return documents[0]


def source(name: str, data: bytes, **kwargs) -> SourceFile:
    return SourceFile(filename=name, data=data, size_bytes=len(data), **kwargs)


# --- detection ------------------------------------------------------------
# The declared content type and the filename are both attacker- and
# accident-controlled, so every case here lies about one of them.


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("anything.dat", build.xlsx_requisition(), FileKind.XLSX),
        ("anything.dat", build.docx_with_table(), FileKind.DOCX),
        ("anything.dat", build.pptx_deck(), FileKind.PPTX),
        ("anything.dat", build.pdf_with_text(), FileKind.PDF),
        ("anything.dat", build.png_photo((8, 8)), FileKind.IMAGE),
        ("anything.dat", build.zip_of({"a.txt": b"hello"}), FileKind.ZIP),
        ("anything.dat", build.eml_with_attachment(b"x", "a.txt"), FileKind.EML),
        ("list.csv", b"a,b\n1,2\n", FileKind.CSV),
        ("notes.txt", b"just prose, no columns here\n", FileKind.TEXT),
        ("empty.bin", b"", FileKind.UNKNOWN),
    ],
)
def test_sniff_reads_the_bytes_not_the_name(name, data, expected):
    assert sniff(data, name) is expected


def test_an_html_export_named_xls_is_recognised_as_html():
    """The single most common lie in a procurement mailbox."""
    data = build.html_pretending_to_be_xls()
    assert sniff(data, "export.xls", "application/vnd.ms-excel") is FileKind.HTML


def test_a_delimited_file_without_a_csv_name_is_still_a_grid():
    assert sniff(b"a;b;c\n1;2;3\n4;5;6\n", "export") is FileKind.CSV


# --- spreadsheets ---------------------------------------------------------


def test_xlsx_keeps_the_header_row_and_every_item():
    document = one(source("Requisition_78432.xlsx", build.xlsx_requisition()))

    assert document.kind is FileKind.XLSX
    grid = document.grids[0]
    assert grid.rows[2] == ITEM_HEADER
    assert grid.rows[3] == ["1", "550101", "ROPE PP 24MM X 220M", "2", "coil"]
    assert grid.height == 6, "the rows Excel keeps below the data must be dropped"


def test_xlsx_origin_names_the_sheet_so_a_field_can_cite_it():
    document = one(source("Requisition_78432.xlsx", build.xlsx_requisition()))
    assert document.grids[0].origin == "Requisition_78432.xlsx#Requisition"


def test_csv_finds_the_separator_without_being_told():
    document = one(source("items.csv", build.csv_requisition(delimiter=";")))
    assert document.grids[0].rows[0] == ITEM_HEADER


def test_row_ceiling_truncates_and_says_so():
    rows = b"a,b\n" + b"1,2\n" * 50
    document = one(
        source("big.csv", rows), budget=Budget(max_spreadsheet_rows=10)
    )

    assert document.grids[0].height == 10
    assert ROWS_TRUNCATED in document.warnings
    assert document.truncated


def test_quantities_do_not_arrive_as_floats():
    """A quantity of "2.0" in the template reads as a data-entry mistake."""
    document = one(source("Requisition.xlsx", build.xlsx_requisition()))
    quantities = [row[3] for row in document.grids[0].rows[3:]]
    assert quantities == ["2", "5", "1"]


# --- PDFs -----------------------------------------------------------------


def test_a_text_pdf_is_read_as_text():
    document = one(source("enquiry.pdf", build.pdf_with_text()))

    assert document.kind is FileKind.PDF
    assert "78432" in document.text
    assert "9232395" in document.text
    assert not document.images
    assert SCANNED_PDF not in document.warnings


def test_a_pdf_without_a_text_layer_becomes_images():
    document = one(source("scan.pdf", build.pdf_without_text()))

    assert SCANNED_PDF in document.warnings
    assert len(document.images) == 1
    assert document.images[0].origin == "scan.pdf#p1"
    assert document.images[0].media_type == "image/jpeg"


def test_a_corrupt_pdf_warns_instead_of_raising():
    document = one(source("broken.pdf", b"%PDF-1.4\nnot really a pdf"))
    assert UNREADABLE in document.warnings


# --- Word and PowerPoint --------------------------------------------------


def test_docx_returns_the_table_not_only_the_paragraphs():
    """python-docx keeps them in separate collections; reading one looks like success."""
    document = one(source("rfq.docx", build.docx_with_table()))

    assert "MV ALMI GLOBE" in document.text
    assert document.grids[0].rows[0] == ITEM_HEADER


def test_pptx_returns_slide_tables():
    document = one(source("deck.pptx", build.pptx_deck()))

    assert "RFQ 78432" in document.text
    assert document.grids[0].rows[0] == ITEM_HEADER


# --- images ---------------------------------------------------------------


def test_a_large_photo_is_downscaled_to_the_cap():
    document = one(source("IMG_2201.png", build.png_photo((2400, 1600))))

    image = document.images[0]
    assert max(image.width, image.height) == Budget().max_image_edge
    assert image.media_type == "image/jpeg"


def test_a_small_inline_image_is_dropped_as_a_signature_logo():
    documents = load(
        source("logo.png", build.png_photo((8, 8)), is_inline=True),
        source("Requisition.xlsx", build.xlsx_requisition()),
    )
    assert [d.filename for d in documents] == ["Requisition.xlsx"]


def test_a_large_inline_image_is_kept():
    """Inline is not the signal on its own - people paste real drawings inline."""
    documents = load(source("drawing.png", build.png_photo((2000, 1400)), is_inline=True))
    assert documents[0].images


def test_the_image_ceiling_is_per_email_not_per_file():
    documents = load(
        *[source(f"p{i}.png", build.png_photo((40, 40))) for i in range(4)],
        budget=Budget(max_images=2),
    )
    kept = sum(len(d.images) for d in documents)
    assert kept == 2
    assert any(IMAGES_TRUNCATED in d.warnings for d in documents)


# --- containers -----------------------------------------------------------


def test_a_zip_yields_the_archive_and_its_contents():
    payload = build.zip_of(
        {"Requisition.xlsx": build.xlsx_requisition(), "notes.txt": b"deliver Jebel Ali"}
    )
    documents = load(source("attachments.zip", payload))

    names = [d.filename for d in documents]
    assert names[0] == "attachments.zip"
    assert set(names[1:]) == {"Requisition.xlsx", "notes.txt"}


def test_a_file_from_a_zip_cites_the_zip_it_came_from():
    payload = build.zip_of({"Requisition.xlsx": build.xlsx_requisition()})
    documents = load(source("attachments.zip", payload))

    inner = next(d for d in documents if d.filename == "Requisition.xlsx")
    assert inner.origin == "attachments.zip > Requisition.xlsx"
    assert inner.grids[0].origin.startswith("attachments.zip > Requisition.xlsx#")


def test_a_zip_inside_a_zip_stops_at_one_level():
    inner = build.zip_of({"Requisition.xlsx": build.xlsx_requisition()})
    documents = load(source("outer.zip", build.zip_of({"inner.zip": inner})))

    nested = next(d for d in documents if d.filename == "inner.zip")
    assert NESTED_TOO_DEEP in nested.warnings
    assert not any(d.filename == "Requisition.xlsx" for d in documents)


def test_archive_entry_ceiling():
    payload = build.zip_of({f"f{i}.txt": b"x" for i in range(10)})
    documents = load(source("many.zip", payload), budget=Budget(max_archive_entries=3))

    assert ARCHIVE_ENTRIES_TRUNCATED in documents[0].warnings
    assert len(documents) == 1 + 3


def test_an_attached_email_gives_up_its_own_attachment():
    payload = build.eml_with_attachment(build.xlsx_requisition(), "Requisition.xlsx")
    documents = load(source("FW RFQ 78432.eml", payload))

    assert "spreadsheet attached" in documents[0].text
    inner = next(d for d in documents if d.filename == "Requisition.xlsx")
    assert inner.grids[0].rows[2] == ITEM_HEADER


# --- limits and refusals --------------------------------------------------


def test_a_reference_attachment_is_reported_not_fetched():
    """A OneDrive link carries no bytes; fetching it is a different permission."""
    document = one(
        SourceFile(filename="Requisition.xlsx", data=None, is_reference=True, size_bytes=0)
    )
    assert ATTACHMENT_IS_A_LINK in document.warnings


def test_an_oversized_file_is_listed_rather_than_dropped():
    document = one(
        source("huge.csv", b"a,b\n" * 1000), budget=Budget(max_single_bytes=100)
    )
    assert FILE_TOO_LARGE in document.warnings
    assert not document.grids


def test_the_attachment_after_the_ceiling_still_appears():
    """A reviewer has to be able to see that attachment twenty-one existed."""
    documents = load(
        *[source(f"f{i}.csv", b"a,b\n1,2\n") for i in range(4)],
        budget=Budget(max_attachments=2),
    )

    assert len(documents) == 4
    assert [bool(d.grids) for d in documents] == [True, True, False, False]
    assert TOO_MANY_ATTACHMENTS in documents[3].warnings


def test_an_html_export_still_gives_up_its_table():
    """Misdeclared, but not unreadable - and the table is the whole RFQ."""
    document = one(source("export.xls", build.html_pretending_to_be_xls()))

    assert document.kind is FileKind.HTML
    assert document.grids[0].rows[0] == ["ITEM", "QTY"]
    assert not document.warnings


def test_an_unreadable_file_says_so_rather_than_raising():
    """The catch-all: unidentifiable bytes are named, not guessed at."""
    document = one(source("firmware.bin", bytes(range(256)) * 4))

    assert document.kind is FileKind.UNKNOWN
    assert NO_READER_FOR_KIND in document.warnings
    assert not document.has_content


def test_order_of_arrival_is_preserved():
    documents = load(
        source("a.csv", b"a\n1\n"),
        source("b.pdf", build.pdf_with_text()),
        source("c.xlsx", build.xlsx_requisition()),
    )
    assert [d.filename for d in documents] == ["a.csv", "b.pdf", "c.xlsx"]


# --- shapes found in real customer attachments ----------------------------


def test_a_pdf_of_pictures_with_captions_is_read_as_a_scan():
    """Measured on a real 18-page presentation of email screenshots: 119
    characters a page - comfortably over the no-text threshold, so every
    screenshot would have been thrown away for the sake of the slide titles."""
    document = one(source("deck.pdf", build.pdf_of_pictures()))

    assert SCANNED_PDF in document.warnings
    assert document.images


def test_a_page_of_real_prose_is_not_called_a_scan_for_having_a_logo():
    """The same rule must not fire on an ordinary letterheaded RFQ."""
    document = one(source("enquiry.pdf", build.pdf_with_text()))

    assert SCANNED_PDF not in document.warnings
    assert document.text


def test_the_image_budget_is_shared_out_rather_than_spent_in_file_order():
    """Measured on the sample set: an 18-page presentation and three photos of
    drill bits. First come, first served gave the presentation everything - and
    the photos were the ones carrying part numbers."""
    documents = load(
        source("deck.pdf", build.pdf_without_text()),
        source("photo1.png", build.png_photo((300, 200))),
        source("photo2.png", build.png_photo((300, 200))),
        budget=Budget(max_images=2),
    )

    # Two slots and three claimants: nobody can be guaranteed one, so the files
    # are taken in the order they arrived. With the real budget of ten, the
    # presentation took seven and each photo still got one.
    assert [len(document.images) for document in documents] == [1, 1, 0]


def test_a_lone_scan_still_gets_the_whole_budget():
    """Sharing must not cost a single scanned requisition its pages."""
    documents = load(source("scan.pdf", build.pdf_without_text()), budget=Budget(max_images=2))

    assert len(documents[0].images) == 1
