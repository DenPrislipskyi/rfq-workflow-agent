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
