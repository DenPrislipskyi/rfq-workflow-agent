"""The kinds of thing one cell can be given.

Not a plain `str | float | date` union: the workbook writes each of these
differently, and the difference is not something a caller should have to carry.
Text becomes an inline string so `sharedStrings.xml` can be copied byte for
byte; a date becomes a number and needs the cell to carry a date format; and a
blank has to be written out rather than skipped, because the master ships demo
values in four of the cells this stage fills.
"""

from dataclasses import dataclass
from datetime import date, datetime

# Excel counts days from here rather than from the 31st, because the format
# carries a deliberate bug: it counts a 29 February 1900 that never happened,
# which puts every date from March 1900 onward one ahead of the true count.
# Dates in January and February 1900 come out one too high; nobody is quoting
# for a vessel arriving in 1900.
EPOCH = date(1899, 12, 30)

SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class Blank:
    """Nothing, written on purpose - which is not the same as a cell left alone."""


@dataclass(frozen=True, slots=True)
class Text:
    value: str


@dataclass(frozen=True, slots=True)
class Number:
    value: float


@dataclass(frozen=True, slots=True)
class Day:
    """A date. The cell it lands in is given a date format if it has none."""

    value: date


@dataclass(frozen=True, slots=True)
class Moment:
    """A date and a time, for the cells that record when something arrived."""

    value: datetime


type CellValue = Blank | Text | Number | Day | Moment

BLANK = Blank()


def excel_serial(value: date) -> int:
    """The number Excel stores a date as."""
    return (value - EPOCH).days


def excel_timestamp(value: datetime) -> float:
    """The day number plus the fraction of the day, which is how Excel keeps time."""
    seconds = value.hour * 3600 + value.minute * 60 + value.second
    return excel_serial(value.date()) + seconds / SECONDS_PER_DAY
