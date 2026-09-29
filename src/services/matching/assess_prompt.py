"""The prompt that reads a line of an RFQ against its shortlist.

The model is shown the customer's line and the descriptions the search found
for it - never an item code, never the rest of the catalogue - and asked what
it sees: whether each candidate is the same kind of product, and how the two
sides state each property that tells such products apart. It gives no score.
The score is worked out from these observations (`confidence.py`), so the
model cannot talk a candidate up, only describe it.

Beside each shortlist goes what its candidates differ in, worked out here word
by word. It decides nothing - it is a pointer, so that the one word telling
four sneakers apart is not the word the model skims past.
"""

from collections.abc import Sequence

from src.domain.rules.catalog import tokenize
from src.infrastructure.llm.client import Messages

SYSTEM = """You compare one line of a ship chandler's purchase request with
the products a catalogue search found for it.

For every candidate you report two things.

1. same_product: is the candidate the same kind of product the line asks for?
   A welding glove and a welding goggle are not; a steel-toe sneaker in size
   25 and one in size 29 are - they are variants of one product.

2. properties: every property that tells products of this kind apart, and
   how each side states it. A property is anything a buyer must get right for
   the delivery to be correct: size, length, diameter, capacity, voltage,
   rating, model or part number, colour, material, grade, flavour, condition,
   brand, pack size.

   For each property give:
   - name: short, lower case, and the same name for the same property on every
     candidate of the line (size, colour, brand, model, voltage, capacity ...)
   - key: true if it decides which product this is (size, model, rating,
     capacity, flavour); false if it only describes it (brand, packaging)
   - line_value: the value as the line states it, or empty if the line does
     not state it
   - item_value: the value as the candidate states it, or empty if it does not
   - agrees: only when both values are given - are they the same value,
     however written? "M14 X 50MM" and "M14*50" are; "25CM" and "25 cm" are;
     "M14" and "M16" are not.

Rules:
- A candidate that is not the same kind of product gets same_product false,
  NO properties, and a why of a few words ("a sugar, not a bacon"). Nothing
  about its properties can change that, so do not spend words on them.
- Keep every why under fifteen words.
- Wording is not a property. Word order, abbreviations ("hex" for
  "hexagon"), plurals, punctuation and reference labels ("RefNo:", "Part
  no.") are not differences.
- Do not invent a value that is not written on that side. Silence is empty.
- List a property if either side states it.
- Answer for every line and every candidate you were given, and only those,
  with the numbers they were given. In `why`, one short sentence on what
  decides this candidate.
"""

EXAMPLES = """Examples:

  line: Steel toe sneakers
    1. SNEAKERS STEEL TOE 25CM
    2. SNEAKERS STEEL TOE 29 CM
  -> 1: same_product true; size: line "", item "25CM"
     2: same_product true; size: line "", item "29 CM"
     (the line names no size, so either could be it)

  line: Hexagon Head Bolts Full Threaded (Bolt with Nut) M14*50
    1. HEX HEAD BOLT/NUT STEEL UNGALV, M14 X 50MM
  -> 1: same_product true; size: line "M14*50", item "M14 X 50MM", agrees true

  line: Battery Free Maintenance | RefNo: Acdelco 12v 200AH
    1. Battery Free Maintenance |12v 200AH
  -> 1: same_product true; voltage: "12v"/"12v" agrees; capacity: "200AH"/"200AH"
     agrees; brand (key false): line "Acdelco", item ""

  line: Ice tea green
    1. ICE TEA, LEMON 24X320 ML LIPTON
  -> 1: same_product true; flavour: line "green", item "LEMON", agrees false

  line: Welding goggles
    1. WELDER GLOVES FIVE FINGERS
  -> 1: same_product false, no properties, why "gloves, not goggles"
"""

INSTRUCTION = (
    "Assess every candidate of every line below. Answer with one entry per "
    "line, carrying the index it was given here, and inside it one entry per "
    "candidate, carrying the number it was given."
)


def build_messages(lines: Sequence[tuple[str, Sequence[str]]]) -> Messages:
    """One prompt for a batch of lines, each with its candidates."""
    body = "\n\n".join(
        _line(index, text, candidates) for index, (text, candidates) in enumerate(lines)
    )
    return [("system", SYSTEM), ("human", f"{INSTRUCTION}\n\n{EXAMPLES}\nLines:\n\n{body}")]


def _line(index: int, text: str, candidates: Sequence[str]) -> str:
    numbered = "\n".join(f"    {number}. {one}" for number, one in enumerate(candidates, start=1))
    hint = differences(candidates)
    tail = f"\n    candidates differ in: {', '.join(hint)}" if hint else ""
    return f"  {index}. line: {text}\n{numbered}{tail}"


def differences(candidates: Sequence[str]) -> list[str]:
    """The words some candidates carry and others do not, in first-seen order.

    Nothing for fewer than two candidates: one product differs from nothing.
    """
    if len(candidates) < 2:
        return []
    words = [tokenize(one) for one in candidates]
    shared = set(words[0]).intersection(*words[1:])
    seen: dict[str, None] = {}
    for tokens in words:
        for word in tokens:
            if word not in shared:
                seen.setdefault(word)
    return list(seen)
