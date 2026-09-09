"""Whether a cell will show a date, and how to make one that will.

The master formats E.T.A and E.T.D as dates but leaves `Quote Before` and
`Requested Delivery` on General - because a person typing a date into a General
cell lets Excel apply a format on the way in. Nothing formats anything for us,
so a serial written into one of those reads `46271`.

The fix is one extra `<xf>`: the cell's own style with a date format added,
appended to `cellXfs` so that no index already in use moves. Styles are shared -
`Quote Before`, `Requested Delivery` and `Delivery Address` are all style 34 -
so the variant is made per style rather than per cell, and the address is left
looking like an address.
"""

import re

# Excel's own formats, which no file has to declare. Only these five show a
# date; 18 to 21 are times of day and 45 to 47 are durations.
_BUILTIN_DATE = frozenset({14, 15, 16, 17, 22})
_BUILTIN_DATE_AND_TIME = frozenset({22})

# What to fall back on when the workbook declares nothing better.
_DEFAULT_DATE = 14
_DEFAULT_DATE_AND_TIME = 22

_CELL_XFS = re.compile(rb"(<cellXfs\b[^>]*>)(.*?)(</cellXfs>)", re.DOTALL)
_XF = re.compile(rb"<xf\b[^>]*?(?:/>|>.*?</xf>)", re.DOTALL)
_NUM_FMT = re.compile(rb'<numFmt\b[^>]*?numFmtId="(\d+)"[^>]*?formatCode="([^"]*)"')
_NUM_FMT_ID = re.compile(rb'\snumFmtId="\d+"')
_COUNT = re.compile(rb'\scount="\d+"')
_APPLY_NUMBER_FORMAT = re.compile(rb'\sapplyNumberFormat="[^"]*"')

# Everything a format code says about itself that is not a date placeholder: a
# locale or colour in brackets, a quoted literal, an escaped character.
_DECORATION = re.compile(r"\[[^\]]*\]|\"[^\"]*\"|\\.")


class StyleTable:
    """One workbook's `cellXfs`, extendable while a sheet is being written.

    Mutable, and made fresh for each file: the styles a workbook needs depend on
    the values going into it, and two RFQs do not need the same ones.
    """

    def __init__(self, xml: bytes) -> None:
        self._xml = xml
        match = _CELL_XFS.search(xml)
        self._entries: list[bytes] = _XF.findall(match.group(2)) if match else []
        self._formats = _formats(xml)
        self._date = _preferred(self._formats, with_time=False)
        self._date_and_time = _preferred(self._formats, with_time=True)
        # (style, wants time) -> the style that is that one, formatted.
        self._variants: dict[tuple[int, bool], int] = {}

    def for_date(self, style: int | None, *, with_time: bool) -> int:
        """The style to give a date in this cell.

        The cell's own when it already shows a date - which is most of them, and
        keeps the master's chosen `d-mmm-yyyy`. Otherwise a copy of it that does.
        """
        index = style or 0
        if index >= len(self._entries):
            # A style the workbook does not define. Excel would ignore it; so do
            # we, rather than cloning something that is not there.
            return index
        if self._shows_a_date(self._numbers_format(index), with_time=with_time):
            return index

        key = (index, with_time)
        if key not in self._variants:
            wanted = self._date_and_time if with_time else self._date
            self._entries.append(_reformatted(self._entries[index], wanted))
            self._variants[key] = len(self._entries) - 1
        return self._variants[key]

    def to_xml(self) -> bytes:
        """The part as it should be written, with any variants appended."""
        match = _CELL_XFS.search(self._xml)
        if match is None or not self._variants:
            # Most workbooks need nothing added, and one that needs nothing
            # should come out of here as the same bytes it went in as.
            return self._xml

        opening = _COUNT.sub(f' count="{len(self._entries)}"'.encode(), match.group(1))
        body = b"".join(self._entries)
        return self._xml[: match.start()] + opening + body + match.group(3) + self._xml[match.end() :]

    def _numbers_format(self, index: int) -> int:
        found = re.search(rb'\snumFmtId="(\d+)"', self._entries[index])
        return int(found.group(1)) if found else 0

    def _shows_a_date(self, number_format: int, *, with_time: bool) -> bool:
        if number_format in self._formats:
            return _is_date(self._formats[number_format], with_time=with_time)
        builtin = _BUILTIN_DATE_AND_TIME if with_time else _BUILTIN_DATE
        return number_format in builtin


def _formats(xml: bytes) -> dict[int, str]:
    """The format codes this workbook declares itself, by id."""
    return {
        int(number): code.decode("utf-8", "replace")
        for number, code in _NUM_FMT.findall(xml)
    }


def _preferred(formats: dict[int, str], *, with_time: bool) -> int:
    """A date format the workbook already uses, or one of Excel's own.

    The master declares `[$-409]d-mmm-yyyy`, which is the shape the mapping
    document asks for, so reusing it beats imposing `mm-dd-yy` from outside.
    """
    for number in sorted(formats):
        if _is_date(formats[number], with_time=with_time):
            return number
    return _DEFAULT_DATE_AND_TIME if with_time else _DEFAULT_DATE


def _is_date(code: str, *, with_time: bool) -> bool:
    """Does this format code put a date on the screen?

    `y` and `d` are unambiguous; `m` is not, because it means minutes as often
    as months. Brackets, quoted text and escapes are stripped first: the `d` in
    a literal "Ordered" is not a day.
    """
    cleaned = _DECORATION.sub("", code).lower()
    shows_date = "y" in cleaned or "d" in cleaned
    return shows_date and ("h" in cleaned if with_time else True)


def _reformatted(entry: bytes, number_format: int) -> bytes:
    """The same style with a different number format, and told to apply it."""
    changed = _NUM_FMT_ID.sub(f' numFmtId="{number_format}"'.encode(), entry, count=1)
    if b"numFmtId=" not in changed:
        changed = changed.replace(b"<xf ", f'<xf numFmtId="{number_format}" '.encode(), 1)
    changed = _APPLY_NUMBER_FORMAT.sub(b"", changed)
    return changed.replace(b"<xf ", b'<xf applyNumberFormat="1" ', 1)
