"""The prompt that asks whether a customer's line and a product are the same thing.

One job, and a narrow one. The left sentence is a line of an RFQ, exactly as the
customer wrote it; the right is the product the code they quoted leads to. The
question is only whether that code answers that line.

The model is shown no codes, no catalogue and no other candidate, so there is
nothing here for it to invent: the answer is a boolean.

The rules are ordered because they conflict on purpose. Almost every pair
differs in wording, and some of those differences are the product. Which kind a
difference is, is the whole task.
"""

from collections.abc import Sequence

from src.infrastructure.llm.client import Messages

SYSTEM = """You decide whether a purchase-request line and a product are the same thing.

The left sentence is one line of a ship chandler's customer request, in the
customer's own words. The right sentence is the product their quoted item code
leads to in the chandler's catalogue.

A code is one claim about what was wanted and the words beside it are another.
Where they disagree the words win, because the words are what the customer is
actually asking for - so your job is to find the disagreements.

Decide by one question: is the item the same thing the line asks for?

Not whether it is close, not whether it would do, not whether a buyer might
accept it. Somebody downstream will place an order against your answer, and a
near miss is a wrong delivery.

Rules, in order of importance:

1. DIFFERENT if the line and the item state different values for the same
   property. Work from the principle, not from a list: a property is anything
   a buyer would have to get right for the delivery to be correct - how big,
   how long, how heavy, how many, what colour, what material or grade, what
   rating, what flavour or type, what condition, which model or part number,
   what the package includes.

   Apply it as arithmetic, not as judgement. If the line says one value and
   the item says another value of the same property, they are different
   products - however near the two values look, however small the gap, and
   whether or not you would call the difference important. You are not asked
   whether a buyer could make do; you are asked whether it is the same thing.

2. Silence is not disagreement. A property the line does not mention cannot
   contradict anything, so an item that names more than the line asked for is
   still the same product. Only two stated values can disagree.

3. SAME if the only difference is wording: word order, abbreviation,
   punctuation, upper case, where the brand sits, or detail the item adds.

4. DIFFERENT if the item side is a placeholder, a category or a heading
   rather than a product - "Not visible", "PROVISION ASSORTED ITEMS",
   "XXBread". You cannot confirm what you cannot read.

5. Answer for every index you were given, and only for those. No prose.
"""

# Six pairs the desk has actually seen, and what each of them is. Examples
# rather than description: where rule 1 and rule 2 pull against each other -
# cheese against ice tea, both differing by a word in the middle - only an
# example says which way it goes.
EXAMPLES = """Examples. Each one shows a rule, not a fact to memorise:

  line: External hard disk drive 4TB
  item: ROD FISHING WITH FURTHER, DETAILS                              -> no
        a different thing entirely

  line: Weldings gloves(five fingers)
  item: WELDER GLOVES FIVE FINGERS                                     -> yes
        same thing, different wording

  line: Kraft cheese slices
  item: CHEESE, SLICED 200 GRM KRAFT                                   -> yes
        the line states no weight, so 200 GRM contradicts nothing

  line: Ice tea green
  item: ICE TEA, LEMON 24X320 ML LIPTON                                -> no
        both state a flavour, and the two differ

  line: Hexagon head bolts with nut, full thread M20 x 80
  item: HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM                     -> no
        both state a size, and the two differ

  line: Safety helmet, white
  item: SAFETY HELMET, BLUE                                            -> no
        both state a colour, and the two differ

  line: Beef tail, frozen 1 KG
  item: BEEF TAIL, FRESH 1 KG                                          -> no
        both state a condition, and the two differ

  line: Face shield for protection with cryo liquids
  item: XXSafety Equipment                                             -> no
        a category, not a product
"""

INSTRUCTION = (
    "Judge each pair below. Answer with one entry per pair, carrying the same "
    "index it was given here."
)


def build_messages(pairs: Sequence[tuple[str, str]]) -> Messages:
    """One prompt for a batch of pairs."""
    numbered = "\n".join(
        f"  {index}. line: {line}\n     item: {item}"
        for index, (line, item) in enumerate(pairs)
    )
    body = f"{INSTRUCTION}\n\n{EXAMPLES}\nPairs:\n\n{numbered}"
    return [("system", SYSTEM), ("human", body)]
