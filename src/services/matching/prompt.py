"""The prompt that asks whether two descriptions are one product.

One job, and a narrow one. Both sentences come from a single row of the desk's
mapping sheet - the left is how a customer once asked for something, the right
is the item the desk mapped it to - and the question is only whether that
mapping is sound. The model is shown no codes, no catalogue and no RFQ, so
there is nothing here for it to invent: the answer is a boolean.

The rules are ordered because they conflict on purpose. Almost every pair
differs in wording, and some of those differences are the product. Which kind
a difference is, is the whole task.
"""

from collections.abc import Sequence

from src.infrastructure.llm.client import Messages

SYSTEM = """You decide whether two descriptions name the same purchasable product.

Both come from one row of a ship chandler's mapping sheet: the left is how a
customer asked for something, the right is the item the desk mapped it to.
The sheet contains mistakes, and finding them is the whole job.

Decide by one question: would a buyer who ordered the left one accept the
right one as what they ordered?

Rules, in order of importance:

1. DIFFERENT if any identifying attribute contradicts: size, dimension,
   weight, volume, capacity, voltage, power, flavour, variant, material,
   model or part number. "ICE TEA GREEN" and "ICE TEA, LEMON" are different
   products. "M16 X 65" and "M20 X 80" are different products. "250 GRM" and
   "400 GRM" are different products. "3XL" and "2XL" are different products.
2. SAME if the only difference is wording: word order, abbreviation,
   punctuation, upper case, where the brand sits, or extra detail on one
   side that does not contradict the other.
3. DIFFERENT if either side is a placeholder, a category or a heading rather
   than a product - "Not visible", "PROVISION ASSORTED ITEMS", "XXBread".
   You cannot confirm what you cannot read.
4. Answer for every index you were given, and only for those. No prose.
"""

# Six rows the desk actually mapped, and what each of them is. Examples rather
# than description: every rule above is visible in them, and where rule 1 and
# rule 2 pull against each other - cheese against ice tea, both of which differ
# by a word in the middle - only an example says which way it goes.
EXAMPLES = """Examples:

  EXTERNAL HDD 4TB               | ROD FISHING WITH FURTHER, DETAILS    -> no
  Weldings gloves(five fingers)  | WELDER GLOVES FIVE FINGERS           -> yes
  KRAFT CHEESE SLICES            | CHEESE, SLICED 200 GRM KRAFT         -> yes
  Ice tea green                  | ICE TEA, LEMON 24X320 ML LIPTON      -> no
  UNSALTED BUTTER 250 GRM LURPAK | BUTTER, UNSALTED, FROZEN 400 GRM LURPAK -> no
  HandWashLiquid w/PumpBottl 200ml [Dettol] | PROVISION ASSORTED ITEMS  -> no
"""

INSTRUCTION = (
    "Judge each pair below. Answer with one entry per pair, carrying the same "
    "index it was given here."
)


def build_messages(pairs: Sequence[tuple[str, str]]) -> Messages:
    """One prompt for a batch of pairs."""
    numbered = "\n".join(
        f"  {index}. customer: {customer}\n     item:     {item}"
        for index, (customer, item) in enumerate(pairs)
    )
    body = f"{INSTRUCTION}\n\n{EXAMPLES}\nPairs:\n\n{numbered}"
    return [("system", SYSTEM), ("human", body)]
