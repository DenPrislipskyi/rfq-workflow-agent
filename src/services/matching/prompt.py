"""The prompt that restates a customer's wording in our own.

One job, and a narrow one: say the same thing in the vocabulary the shelf uses.
Not to identify the product, not to guess what was meant, not to fill anything
in - the catalogue search that follows does the identifying, and it can only be
misled by a line that now says something the customer never did.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from src.infrastructure.llm.client import Messages

if TYPE_CHECKING:
    from src.services.matching.models import Question

SYSTEM = """You restate purchase-request lines for a marine ship chandler.

A customer's wording and a chandler's catalogue describe the same goods in
different words: "Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65" and
"HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM" are one product. Your job is to
write the customer's line the way the catalogue would, so that the two can be
compared.

Rules, in order of importance:

1. Never invent a product. You restate; you do not identify. If a line is too
   vague to restate, return it unchanged.
2. Keep every number, size, dimension, voltage, capacity, standard and brand
   exactly as written. "M16*65" may become "M16 X 65MM"; it may never become
   "M16" or "M20".
3. Lead with what the thing is, then its qualifiers, then its size. "Convex
   rulers" -> "RULE CONVEX". "Steel toe sneakers 25" -> "SNEAKERS STEEL TOE 25".
4. Drop only what does not identify the product: "please quote", "with further
   details", "as per attached", quantities, prices, delivery notes, and the
   customer's own reference numbers unless they are a manufacturer's part
   number.
5. Answer in English, upper case, one line per input line. No explanation.
"""

# Three lines the desk has actually mapped, and the wording they mapped them to.
# Examples rather than description: what "the way the catalogue would" means is
# far easier to show than to specify, and every rule above is visible in them.
EXAMPLES = """Examples:

  1. Weldings gloves(five fingers)
     -> WELDER GLOVES FIVE FINGERS
  2. Battery Free Maintenance | RefNo: Acdelco 12v 200AH
     -> BATTERY MAINTENANCE FREE ACDELCO 12V 200AH
  3. Torch light with charger | RefNo.GFL3801N
     -> TORCH LIGHT WITH CHARGER GFL3801N
"""

INSTRUCTION = (
    "Restate each line below. Answer with one entry per line, carrying the same "
    "index it was given here."
)


def build_messages(wordings: Sequence[str]) -> Messages:
    """One prompt for a batch of lines."""
    numbered = "\n".join(f"  {index}. {text}" for index, text in enumerate(wordings))
    body = f"{INSTRUCTION}\n\n{EXAMPLES}\nLines:\n\n{numbered}"
    return [("system", SYSTEM), ("human", body)]


CHOICE_SYSTEM = """You match purchase-request lines to a ship chandler's own
product list.

For each line you are shown what the customer asked for and a short list of
products from our catalogue. Choose the one product that is what they asked
for, or choose none.

Rules, in order of importance:

1. "None" is a correct answer. A blank line costs an operator one search; a
   wrong product costs a wrong delivery to a ship that has sailed. When no
   candidate is the same product, say so.
2. Same product means the same thing, in the same specification. A different
   size, length, capacity, voltage, material or grade is a DIFFERENT product:
   M16 x 65 is not M20 x 80, 200ml is not 500ml, and 2XL is not 3XL.
3. Where a candidate is marked "by customer code", the customer quoted a code
   that leads to it. That is a hint, not an answer. If its description is a
   different product from what the line describes, reject it - customers do
   quote codes that lead somewhere else.
4. Answer only with item codes shown for that same line. Never with a code from
   another line, and never with one you have not been shown.
5. Give one short sentence saying why. For a refusal, say what was missing or
   what differed.
6. Score every candidate you were shown from 0 to 100 - how sure you are that
   it is the product asked for - including the ones you did not choose. A
   refusal with every candidate at 10 says something different from one with a
   candidate at 70, and the person reviewing it needs to see which it was.
"""

CHOICE_INSTRUCTION = (
    "Choose one product per line, or none. Answer with one entry per line, "
    "carrying the index it was given here."
)


def build_choice_messages(questions: Sequence["Question"]) -> Messages:
    """One prompt for a batch of lines, each with its own candidates."""
    body = "\n\n".join(_question_block(question) for question in questions)
    return [("system", CHOICE_SYSTEM), ("human", f"{CHOICE_INSTRUCTION}\n\n{body}")]


def _question_block(question: "Question") -> str:
    """One line and everything the catalogue could offer for it."""
    lines = [f"[{question.index}] Customer asked for: {question.description}"]
    if question.verbatim and question.verbatim != question.description:
        lines.append(f"      as they wrote it: {question.verbatim}")
    if not question.candidates:
        lines.append("      candidates: none - the catalogue has nothing like it")
        return "\n".join(lines)

    lines.append("      candidates:")
    for candidate in question.candidates:
        mark = " (by customer code)" if candidate.code == question.by_code else ""
        lines.append(f"        {candidate.code}{mark}  {candidate.description}")
    return "\n".join(lines)
