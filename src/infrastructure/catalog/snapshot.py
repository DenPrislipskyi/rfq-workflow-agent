"""The last catalogue we successfully read, kept on disk.

It exists so that the agent never depends on Google being up at the moment an
email arrives, and so that "why did it pick that code last Tuesday?" has an
answer: the sheet has moved on, the snapshot has not.

It also decides one thing on the way in. Codes are text - `0012345` and
`1.23457E+11` are both codes a spreadsheet will happily hand over as numbers -
so every cell is read as the string it was written as, and nothing here is
converted to anything.
"""

import csv
import hashlib
import io
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_csv(text: str) -> list[dict[str, str]]:
    """CSV text as rows keyed by column heading, every cell a string.

    A function rather than a method: both the file on disk and the answer that
    has just come off the wire are the same format, and only one of them is a
    snapshot.
    """
    reader = csv.DictReader(io.StringIO(text))
    return [
        {key: (value or "").strip() for key, value in row.items() if key}
        for row in reader
    ]


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """When this copy was taken, and what it holds."""

    fetched_at: str
    rows: int
    sha256: str

    @property
    def age(self) -> str:
        taken = datetime.fromisoformat(self.fetched_at)
        hours = (datetime.now(UTC) - taken).total_seconds() / 3600
        return f"{hours:.1f} h"


class Snapshot:
    """A CSV file, and a small note beside it saying where it came from."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._note = path.with_suffix(".json")

    @property
    def path(self) -> Path:
        return self._path

    def write(self, text: str) -> SnapshotInfo:
        """Replace the snapshot, whole or not at all."""
        info = SnapshotInfo(
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            rows=len(parse_csv(text)),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".csv.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self._path)
        self._note.write_text(json.dumps(asdict(info), indent=2), encoding="utf-8")

        return info

    def rows(self) -> list[dict[str, str]]:
        """Every row, by column heading. Empty when there is no snapshot yet."""
        if not self._path.is_file():
            return []
        return parse_csv(self._path.read_text(encoding="utf-8"))

    def info(self) -> SnapshotInfo | None:
        if not self._note.is_file():
            return None
        try:
            return SnapshotInfo(**json.loads(self._note.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError):
            logger.exception("Could not read %s", self._note)
            return None
