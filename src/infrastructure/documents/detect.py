"""What a file actually is, decided from its bytes.

The `contentType` Graph reports is set by the sender's mail client and verified
by nobody. `application/octet-stream` is the usual fallback and says nothing;
portal and ERP exports routinely ship HTML under an `.xls` name; renaming an
extension is free. So the bytes decide, the declared type breaks ties, and the
filename is consulted last.

Microsoft's own attachment filter works the same way and calls it true type
matching - by the leading and trailing bytes of the file, regardless of the
filename extension.

No model is involved here or ever should be. This is a lookup table.
"""

import io
import re
import zipfile

import olefile

from src.infrastructure.documents.models import FileKind

# A zip local-file header, an empty archive, and a spanned archive.
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

_IMAGE_MAGICS: tuple[tuple[bytes, int], ...] = (
    (b"\xff\xd8\xff", 0),  # jpeg
    (b"\x89PNG\r\n\x1a\n", 0),  # png
    (b"GIF87a", 0),
    (b"GIF89a", 0),
    (b"BM", 0),  # bmp
    (b"II*\x00", 0),  # tiff, little endian
    (b"MM\x00*", 0),  # tiff, big endian
)

# HEIC/HEIF and friends: an ISO-BMFF `ftyp` box whose brand names the format.
_HEIF_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"})

# The first line of an RFC 5322 message. Not every .eml starts with the same
# header, so match the shape rather than one key.
_EMAIL_FIRST_LINE = re.compile(
    rb"^(?:Received|Return-Path|From|To|Subject|Date|Message-ID|MIME-Version|"
    rb"X-[A-Za-z0-9-]+|Delivered-To|Content-Type):\s",
    re.IGNORECASE,
)

_HTML_START = re.compile(rb"^\s*(?:<!doctype\s+html|<html\b|<head\b|<body\b)", re.IGNORECASE)
_XML_START = re.compile(rb"^\s*<\?xml\b", re.IGNORECASE)

# %PDF- belongs at offset 0, but files that went through a mail gateway
# sometimes carry a few junk bytes in front. Adobe's own readers scan this far.
_PDF_SCAN_WINDOW = 1024

_TEXT_EXTENSIONS = {".csv": FileKind.CSV, ".tsv": FileKind.CSV, ".txt": FileKind.TEXT}


def sniff(
    data: bytes,
    filename: str | None = None,
    declared_type: str | None = None,
) -> FileKind:
    """Identify `data`. Never raises - an unidentifiable file is `UNKNOWN`."""
    if not data:
        return FileKind.UNKNOWN

    if data.startswith(_ZIP_MAGICS):
        return _inside_zip(data)

    if olefile.isOleFile(io.BytesIO(data)):
        return _inside_ole(data)

    if b"%PDF-" in data[:_PDF_SCAN_WINDOW]:
        return FileKind.PDF

    if _is_image(data):
        return FileKind.IMAGE

    return _text_like(data, filename, declared_type)


def _inside_zip(data: bytes) -> FileKind:
    """OOXML formats are all zips; the entry names say which one.

    A plain `.zip` of customer files looks identical from the first four bytes,
    so the archive has to be opened to tell an RFQ spreadsheet from a folder of
    drawings.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return FileKind.UNKNOWN

    if "xl/workbook.xml" in names:
        return FileKind.XLSX
    if "word/document.xml" in names:
        return FileKind.DOCX
    if "ppt/presentation.xml" in names:
        return FileKind.PPTX
    return FileKind.ZIP


def _inside_ole(data: bytes) -> FileKind:
    """The old Microsoft container: .xls, .doc and .msg share the same magic.

    The stream names inside are what separate them.
    """
    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            streams = {"/".join(entry).lower() for entry in ole.listdir()}
    except Exception:
        return FileKind.UNKNOWN

    if any(name.startswith("__properties_version") for name in streams):
        return FileKind.MSG
    if "workbook" in streams or "book" in streams:
        return FileKind.XLS
    if "worddocument" in streams:
        return FileKind.DOC
    return FileKind.UNKNOWN


def _is_image(data: bytes) -> bool:
    if any(data.startswith(magic) for magic, _ in _IMAGE_MAGICS):
        return True
    if data[4:8] == b"ftyp" and data[8:12].lower() in _HEIF_BRANDS:
        return True
    # WebP is a RIFF container; the fourth word names the payload.
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _text_like(
    data: bytes, filename: str | None, declared_type: str | None
) -> FileKind:
    """Whatever is left: markup, a delimited table, plain prose, or not text at all."""
    head = data[:8192].lstrip(b"\xef\xbb\xbf")

    if _HTML_START.match(head) or _XML_START.match(head):
        return FileKind.HTML
    if _EMAIL_FIRST_LINE.match(head):
        return FileKind.EML
    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        return FileKind.SEVEN_ZIP

    if not _is_probably_text(head):
        return FileKind.UNKNOWN

    # Text confirmed. Only now may the name and the declared type have a say -
    # a .csv and a .txt are the same bytes and differ by intent alone.
    if filename:
        suffix = filename[filename.rfind(".") :].lower() if "." in filename else ""
        if suffix in _TEXT_EXTENSIONS:
            return _TEXT_EXTENSIONS[suffix]
    if declared_type and "csv" in declared_type.lower():
        return FileKind.CSV

    return FileKind.CSV if _looks_delimited(head) else FileKind.TEXT


def _is_probably_text(head: bytes) -> bool:
    """No NUL bytes and decodable. Cheap, and wrong only on exotic encodings."""
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("cp1252")
        except UnicodeDecodeError:
            return False
    return True


def _looks_delimited(head: bytes) -> bool:
    """Two or more lines carrying the same separator the same number of times."""
    lines = [line for line in head.decode("utf-8", "replace").split("\n")[:5] if line.strip()]
    if len(lines) < 2:
        return False
    return any(
        lines[0].count(sep) >= 1 and len({line.count(sep) for line in lines}) == 1
        for sep in (",", ";", "\t", "|")
    )
