"""B6: put what the customer wrote into the shape the cell can hold.

No model runs here, and none should. Every question at this stage has an exact
answer - a check digit, a calendar, a list of three-letter codes - and a model
asked any of them would only be a slower way of being occasionally wrong.

**What the customer wrote is what goes in the form.** The only changes are the
ones a cell forces: `28/07` becomes a date because a date cell cannot hold text,
`eur` becomes `EUR` and `$` becomes `USD` because the currency cell holds three
letters. Nothing else is rewritten, and nothing is dropped for being unfamiliar
- `Gdansk` goes in as `Gdansk`.

One thing is left blank rather than guessed: a date nobody could read. `03/04`
is the third of April or the fourth of March, and a date cell has no way to say
"one of these two". `28/07` is not one of those - the 28th cannot be a month, so
the order is settled and the year comes from the same rule that reads `12 Oct`.

Every blank cell owes an explanation, and there are three different ones here:
the value could be read two ways, it could not be read at all, or it read
perfectly and is not a day - `31/02`. The last is the customer's typo rather
than our limit, and a person told which one it is knows where to look.
"""

import logging
import re
from datetime import date

from src.services.extraction.models import (
    DATE_AMBIGUOUS,
    DATE_IMPOSSIBLE,
    DATE_UNREADABLE,
    IMO_INVALID,
    RFQ_TYPE_FELL_BACK,
    HeaderField,
    NormalizedHeader,
    RfqHeader,
)

logger = logging.getLogger(__name__)

# A date this far in the past was meant for next year: RFQ dates look forward,
# and "12 Oct" written in November means the October after this one.
BACKDATING_TOLERANCE_DAYS = 60

DATE_FIELDS = (
    HeaderField.ETA,
    HeaderField.ETD,
    HeaderField.QUOTE_BEFORE,
    HeaderField.REQUESTED_DELIVERY,
)
# Copied through with nothing but whitespace trimmed.
FREE_TEXT_FIELDS = (
    HeaderField.VESSEL_NAME,
    HeaderField.RFQ_REFERENCE,
    HeaderField.CUSTOMER_CONTACT,
    HeaderField.CUSTOMER_PHONE,
    HeaderField.CUSTOMER_EMAIL,
    HeaderField.PERSON_DESIGNATION,
    HeaderField.DELIVERY_ADDRESS,
)

# What a currency symbol means. Only the four the template offers.
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "US$": "USD", "USD$": "USD", "AED": "AED", "DHS": "AED"}

# The bucket the template provides for "genuinely mixed or unclear".
FALLBACK_RFQ_TYPE = "OTHERS"

# The twelve the form's dropdown holds and the three-letter currency codes it
# accepts. Written here as well as read from the master, because the form has
# to be filled correctly whether or not the master is being consulted - and
# neither list has changed in the two versions of the workbook we have seen.
RFQ_TYPES = [
    "BOND", "CABIN", "DECK", "ENGINE", "PROVISION", "ELECTRICAL",
    "MEDICAL", "SAFETY", "STATIONARY", "PRIVATE", "OTHERS", "TENDER",
]
CURRENCY_CODES = ["AED", "EGP", "EUR", "JPY", "KRW", "OMR", "SGD", "USD"]

_MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"),
            ("apr", "april"), ("may",), ("jun", "june"),
            ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
            ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$")
# `28/07`, with the year left off. Real: "The requested delivery date is 28/07".
_DAY_AND_MONTH = re.compile(r"^(\d{1,2})[./-](\d{1,2})$")
_DAY_MONTH = re.compile(r"^(\d{1,2})[\s./-]*([A-Za-z]{3,9})\.?[\s,./-]*(\d{2,4})?$")
_MONTH_DAY = re.compile(r"^([A-Za-z]{3,9})\.?[\s,./-]*(\d{1,2})[\s,./-]*(\d{2,4})?$")
# Everything that is not a letter or a digit, in any script. Deliberately not
# `[^a-z0-9]`: the master's client list has 2,190 names and some of them are
# Korean, which that pattern erased to the empty string - and an empty string is
# contained in every other name, so one Korean client matched every query.
_NOT_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalize_header(
    header: RfqHeader, *, today: date | None = None
) -> NormalizedHeader:
    """Map a verbatim header onto the template's cells.

    `branch` decides which port list applies, so it has to be resolved before
    this runs - the region rules already do that from the email.
    """
    text: dict[HeaderField, str] = {}
    dates: dict[HeaderField, date] = {}
    warnings: list[str] = []
    dropped: dict[HeaderField, str] = {}
    checked: dict[HeaderField, str] = {}

    def warn(reason: str) -> None:
        if reason and reason not in warnings:
            warnings.append(reason)

    def keep(name: HeaderField, value: str | None, reason: str = "") -> None:
        """One field, or the reason it is not there.

        `reason` is what to say when the value did not survive. A field the
        customer never wrote produces nothing at all - there is no explaining
        a cell nobody could have filled.
        """
        if value:
            text[name] = value
        elif header.value(name) is not None:
            dropped[name] = reason
            warn(reason)

    def flag(name: HeaderField, reason: str) -> None:
        """Written, and worth a second look.

        The mapping document asks for a best candidate rather than a blank
        where several entries look alike, and a value nobody is told to check
        is a value nobody checks.
        """
        if reason:
            checked[name] = reason
            warn(reason)

    for name in FREE_TEXT_FIELDS:
        keep(name, (header.value(name) or "").strip() or None)

    written_imo = header.value(HeaderField.IMO)
    imo = normalize_imo(written_imo)
    if imo:
        keep(HeaderField.IMO, imo)
    elif written_imo:
        # "If mismatch -> still populate extracted IMO + flag", says the mapping
        # document, and it is right: a check digit catches a typo, and the
        # number with the typo still tells a person which vessel was meant.
        keep(HeaderField.IMO, written_imo.strip())
        flag(HeaderField.IMO, IMO_INVALID)

    keep(HeaderField.DELIVERY_PORT, (header.value(HeaderField.DELIVERY_PORT) or "").strip() or None)

    written_currency = header.value(HeaderField.CURRENCY)
    if written_currency:
        # Three letters, because that is what the cell holds. `$` is not one.
        keep(
            HeaderField.CURRENCY,
            normalize_currency(written_currency, CURRENCY_CODES)
            or written_currency.strip().upper(),
        )

    rfq_type, fell_back = normalize_rfq_type(header.value(HeaderField.RFQ_TYPE), RFQ_TYPES)
    keep(HeaderField.RFQ_TYPE, rfq_type)
    if fell_back:
        warn(RFQ_TYPE_FELL_BACK)

    for name in DATE_FIELDS:
        raw = header.value(name)
        if raw is None:
            continue
        parsed, problem = parse_date(raw, today=today)
        if parsed:
            dates[name] = parsed
        else:
            dropped[name] = problem
            warn(problem)

    return NormalizedHeader(
        text=text, dates=dates, warnings=warnings, dropped=dropped, checked=checked
    )


# --- the rest -------------------------------------------------------------


def normalize_imo(value: str | None) -> str | None:
    """Seven digits whose last one checks out.

    The check digit is the sum of the first six weighted 7,6,5,4,3,2, modulo
    ten. It catches a transposed pair, which is the mistake a person retyping
    an IMO actually makes.
    """
    if not value:
        return None

    digits = re.sub(r"\D", "", value)
    if len(digits) != 7:
        return None

    expected = sum(int(digit) * weight for digit, weight in zip(digits[:6], range(7, 1, -1)))
    return digits if expected % 10 == int(digits[6]) else None


def normalize_currency(value: str | None, accepted: list[str]) -> str | None:
    """An ISO code the template offers, or nothing."""
    if not value:
        return None

    cleaned = value.strip().upper()
    if cleaned in accepted:
        return cleaned
    if (mapped := CURRENCY_SYMBOLS.get(cleaned)) and mapped in accepted:
        return mapped

    # "DHS-Un. Ar Emir. Dirham", "USD-US DOLLAR" - the workbook's own spelling.
    for code in accepted:
        if re.match(rf"^{code}\b", cleaned):
            return code
    for symbol, code in CURRENCY_SYMBOLS.items():
        if cleaned.startswith(symbol) and code in accepted:
            return code
    return None


def normalize_rfq_type(value: str | None, accepted: list[str]) -> tuple[str | None, bool]:
    """One of the twelve. Returns the value and whether it fell back to OTHERS.

    The template provides `OTHERS` for exactly this, so an unrecognised
    department is not a blank cell - a blank one is a starred field missing,
    which is worse than the bucket that was put there for the purpose.
    """
    if not value:
        return None, False

    cleaned = value.strip().upper()
    if cleaned in accepted:
        return cleaned, False

    for code in accepted:
        if code in cleaned:
            return code, False

    return (FALLBACK_RFQ_TYPE, True) if FALLBACK_RFQ_TYPE in accepted else (None, False)


def parse_date(value: str, *, today: date | None = None) -> tuple[date | None, str]:
    """A date, or the reason there is not one.

    `03/04/2026` is the third of April in most of the world and the fourth of
    March in some of it, and nothing in an RFQ says which. It is left blank: a
    missing ETA costs somebody one question, a wrong one costs a delivery.

    A trailing separator goes first. The value is quoted verbatim out of the
    customer's prose - "the requested delivery date is 28/07." - so it can
    arrive carrying the sentence's full stop, and none of a date is ever in a
    trailing `.` or `/`.
    """
    cleaned = value.strip().rstrip(" ./-")
    today = today or date.today()

    if match := _ISO.match(cleaned):
        return _real(_build(*(int(part) for part in match.groups())))

    if match := _NUMERIC_DATE.match(cleaned):
        first, second, year = (int(part) for part in match.groups())
        if first > 12 and second <= 12:
            return _real(_build(_year(year), second, first))
        if second > 12 and first <= 12:
            return _real(_build(_year(year), first, second))
        if first == second:
            return _real(_build(_year(year), first, second))
        return None, DATE_AMBIGUOUS

    if match := _DAY_AND_MONTH.match(cleaned):
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12 and second <= 12:
            # `28/07`: the 28th cannot be a month, so the order is settled and
            # the missing year is the one that puts the date in front of us.
            return _real(_with_year(first, second, None, today))
        if second > 12 and first <= 12:
            return _real(_with_year(second, first, None, today))
        return None, DATE_AMBIGUOUS

    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        if match := pattern.match(cleaned):
            day, name, year = match.groups() if order == "dm" else match.group(2, 1, 3)
            month = _MONTHS.get(name.lower())
            if month:
                return _real(_with_year(int(day), month, year, today))

    return None, DATE_UNREADABLE


def _real(day: date | None) -> tuple[date | None, str]:
    """A date, or why a value that read like one is not one.

    `31/02` and `40/07` parse without trouble and are still not days. Calling
    them unreadable would send a person hunting for a parser bug, and saying
    nothing at all - which is what this used to do - leaves a blank cell with
    no explanation beside it.
    """
    return (day, "") if day is not None else (None, DATE_IMPOSSIBLE)


def _with_year(day: int, month: int, year: str | None, today: date) -> date | None:
    """A written year, or the one that puts the date in front of us."""
    if year:
        return _build(_year(int(year)), month, day)

    guess = _build(today.year, month, day)
    if guess is None:
        return None
    if (today - guess).days > BACKDATING_TOLERANCE_DAYS:
        return _build(today.year + 1, month, day)
    return guess


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(value: int) -> int:
    """Two digits mean this century. Nobody is quoting for 1926."""
    return value + 2000 if value < 100 else value


def _key(value: str) -> str:
    """Case, spacing and punctuation removed - what two spellings share."""
    return _NOT_WORD.sub("", value.strip().lower())
