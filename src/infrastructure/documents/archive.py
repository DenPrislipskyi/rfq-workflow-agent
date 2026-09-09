"""Archives, unpacked one level deep.

"Please find attached" plus a zip of eight files is ordinary traffic. What is
not ordinary, and has to be guarded against anyway, is an archive that expands
to gigabytes: the declared uncompressed size in a zip header is written by
whoever built the file, so it is a hint and not a limit. The real total is
counted while reading.

One level only. A zip inside a zip is either a mistake or an attack, and either
way a person should look at it.
"""

import io
import logging
import zipfile
from collections.abc import Iterator

import py7zr

from src.infrastructure.documents.budget import Budget
from src.infrastructure.documents.models import (
    ARCHIVE_ENTRIES_TRUNCATED,
    ARCHIVE_TOO_BIG,
    UNREADABLE,
)

logger = logging.getLogger(__name__)

# Windows zips carry backslashes; entries starting with "__MACOSX" are resource
# forks the sender never meant to send.
_IGNORED_PREFIXES = ("__MACOSX/", ".")


def unpack(
    data: bytes, kind_is_zip: bool, budget: Budget
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Return the entries inside, plus any warning codes the unpacking raised."""
    reader = _unpack_zip if kind_is_zip else _unpack_7z
    entries: list[tuple[str, bytes]] = []
    warnings: list[str] = []
    total = 0

    try:
        for name, payload in reader(data):
            if len(entries) >= budget.max_archive_entries:
                warnings.append(ARCHIVE_ENTRIES_TRUNCATED)
                break

            total += len(payload)
            if total > budget.max_archive_uncompressed:
                warnings.append(ARCHIVE_TOO_BIG)
                break

            entries.append((name, payload))
    except Exception as error:  # noqa: BLE001 - both libraries raise their own types
        warnings.append(UNREADABLE)
        logger.debug("Could not unpack archive: %s", error)

    return entries, warnings


def _unpack_zip(data: bytes) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or _ignored(info.filename):
                continue
            yield _basename(info.filename), archive.read(info)


def _unpack_7z(data: bytes) -> Iterator[tuple[str, bytes]]:
    with py7zr.SevenZipFile(io.BytesIO(data)) as archive:
        for name, stream in (archive.readall() or {}).items():
            if _ignored(name):
                continue
            yield _basename(name), stream.read()


def _ignored(name: str) -> bool:
    return name.startswith(_IGNORED_PREFIXES) or "/." in name


def _basename(name: str) -> str:
    """Flatten the path. Directory structure inside the archive carries no
    meaning for us, and a name like `../../etc/passwd` must never reach disk."""
    return name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or name
