"""Our own product list, and how a customer's wording is matched against it.

Pure: rows in, candidates out. No file, no network, no model - which is what
lets the whole thing be measured against the desk's own finished RFQs in a
test that runs in milliseconds.

Two routes into the catalog, and the difference between them is the point:

    by code          exact, and never enough on its own
    by description   ranked, and the only route most lines have

The desk's own matching notes are explicit about the first one - *"do not trust
the code alone, validate the requested customer description"* - and the two
worked examples they sent are both of a customer code that leads somewhere
else: `110188` asks for an external hard drive and names a fishing rod. So a
code hit is a candidate like any other, and `Shortlist` says whether the
description search found the same item. Where the two disagree, somebody has to
look; nothing here decides that on its own.

Ranking is BM25 over the words of the description. It is the standard answer to
this exact question, it is twenty lines, and it needs no dependency: a
catalogue of tens of thousands of short descriptions is scored in a millisecond
from an inverted index built once at load.
"""

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# BM25's two knobs, at the values the literature settles on. `k1` bounds what
# repeating a word can buy - nothing, in descriptions this short - and `b` is
# how hard a long description is penalised for being long.
K1 = 1.2
B = 0.75

# Everything that is not a letter or a digit splits words. "M16 x 65mm" becomes
# "m16 x 65mm": the sizes stay attached to their numbers, which is exactly what
# tells two bolts apart.
_SPLIT = re.compile(r"[\W_]+", re.UNICODE)
# A code is compared without the punctuation people put in it: "T-691284/00"
# and "t69128400" are one code written twice.
_NOT_ALPHANUMERIC = re.compile(r"[\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """The words of a description, lowercased and cut back to a common stem.

    Unicode-aware on purpose. An earlier version of this idea elsewhere in the
    project stripped everything outside `[a-z0-9]`, which turned Korean names
    into the empty string - and an empty key matches everything.
    """
    return [_stem(word) for word in _SPLIT.split(text.lower()) if word]


def _stem(word: str) -> str:
    """Enough English to survive the gap between two people's wording.

    A customer writes "welding goggles"; the shelf says "GOGGLE WELDER". Three
    endings account for nearly all of that - a plural, a participle, a past
    tense - and every rule keeps a stem long enough to still mean something, so
    that "spring" is not filed under "spr" or "gas" under "ga".

    Deliberately not a real stemmer. Both sides of every comparison go through
    this same function, so what it gets wrong it gets wrong symmetrically, and
    what it cannot do - "boxes" against "box" - is a miss rather than a wrong
    answer. Which of the two matters is a question for `catalog_recall`.
    """
    if len(word) >= 5 and word.endswith("ies"):
        return word[:-3] + "y"
    for ending, keep in (("ing", 4), ("ed", 4)):
        if word.endswith(ending) and len(word) - len(ending) >= keep:
            return word[: -len(ending)]
    if word.endswith("s") and not word.endswith("ss") and len(word) - 1 >= 3:
        return word[:-1]
    return word


def normalize_code(code: str) -> str:
    """A code as a key: upper case, and nothing but letters and digits."""
    return _NOT_ALPHANUMERIC.sub("", code).upper()


@dataclass(frozen=True, slots=True)
class CatalogItem:
    """One product, as our own list describes it."""

    code: str
    description: str
    # How a customer once asked for this same product, in their own words. The
    # sheet is a history of mappings, so this is the wording an incoming email
    # is most likely to resemble - and it is indexed beside our own.
    customer_description: str = ""
    # What the customer calls the same thing - their own code, or an IMPA
    # number. Ours is the key; this is a second way in, because a customer who
    # quotes anything usually quotes this one.
    customer_code: str = ""
    # Every other column of the row, by its heading. Kept whole rather than
    # picked apart: the sheet grows columns - UOM, pack size, stock - and this
    # module has no business deciding in advance which of them matter.
    fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One item the search thinks could be what the customer asked for."""

    item: CatalogItem
    score: float


@dataclass(frozen=True, slots=True)
class Shortlist:
    """What the catalogue can say about one line of an RFQ, without a model.

    `by_code` is the exact hit on the code the customer quoted, when there was
    one. `confirmed` is the answer to the desk's own question about it: did
    searching the customer's *words* find that same item? False means the code
    and the description point at different products, which is the case their
    notes single out and the one a person has to settle.
    """

    candidates: Sequence[Candidate] = ()
    by_code: CatalogItem | None = None
    confirmed: bool | None = None

    @property
    def best(self) -> CatalogItem | None:
        return self.candidates[0].item if self.candidates else None

    @property
    def codes(self) -> list[str]:
        return [candidate.item.code for candidate in self.candidates]

    @property
    def conflicted(self) -> bool:
        """A code was quoted, and the words say it is the wrong product."""
        return self.by_code is not None and self.confirmed is False


class Catalog:
    """Our product list, indexed for the two questions anyone asks of it."""

    def __init__(
        self, items: Sequence[CatalogItem], *, index_customer_description: bool = False
    ) -> None:
        self._items = list(items)
        # Whether a customer's past wording is searched, as opposed to merely
        # kept. Two different decisions: the wording is worth having on the
        # item either way - whatever chooses between candidates later should
        # see how this product was once asked for - but whether it helps the
        # ranking is a question for `catalog_recall`, and on a sheet that
        # mentions each product once the answer is no.
        self._index_customer_description = index_customer_description

        # Our own codes are the keys, and the first row wins wherever a code
        # repeats: the sheet is meant to hold each code once, so a repeat is a
        # data error rather than a choice to make. The customers' codes fill
        # the gaps afterwards and never overwrite - if one customer's code
        # happens to be another item's real code, the real one wins.
        self._by_code: dict[str, CatalogItem] = {}
        for item in self._items:
            self._by_code.setdefault(normalize_code(item.code), item)
        for item in self._items:
            alias = normalize_code(item.customer_code) if item.customer_code else ""
            if alias:
                self._by_code.setdefault(alias, item)

        # Inverted index: word -> [(item, how often it says that word)]. Built
        # once, because every line of every RFQ asks the same question of it.
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._lengths: list[int] = []
        for position, item in enumerate(self._items):
            counts: dict[str, int] = defaultdict(int)
            for word in self._words(item):
                counts[word] += 1
            for word, count in counts.items():
                self._postings[word].append((position, count))
            self._lengths.append(sum(counts.values()))

        self._average_length = (sum(self._lengths) / len(self._lengths)) if self._items else 0.0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> Sequence[CatalogItem]:
        return self._items

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, str]],
        *,
        code_column: str,
        description_column: str,
        customer_code_column: str = "",
        customer_description_column: str = "",
        index_customer_description: bool = False,
    ) -> "Catalog":
        """Build from rows of a spreadsheet, by column heading.

        Headings are matched loosely - case and spacing vary between whoever
        typed the sheet and whoever typed the setting - but a row missing a
        code or a description is dropped rather than guessed at: an item
        nothing can be matched against is not an item.

        `customer_code_column` is optional, and worth setting when the sheet
        has one: most customers who quote a code at all quote their own.
        """
        items = []
        for row in rows:
            found = {_heading(key): value for key, value in row.items() if key}
            code = (found.get(_heading(code_column)) or "").strip()
            description = (found.get(_heading(description_column)) or "").strip()
            if code and description:
                items.append(
                    CatalogItem(
                        code=code,
                        description=description,
                        customer_description=_column(found, customer_description_column),
                        customer_code=_column(found, customer_code_column),
                        fields={key: value for key, value in row.items() if key},
                    )
                )
        return cls(items, index_customer_description=index_customer_description)

    def by_code(self, code: str | None) -> CatalogItem | None:
        """The item this code names - ours, or the customer's own.

        Exact, and still only a candidate: what a code names and what the
        customer described are two different claims, and `shortlist` is where
        they are put side by side.
        """
        if not code:
            return None
        return self._by_code.get(normalize_code(code))

    def search(self, text: str, limit: int = 20) -> list[Candidate]:
        """The items whose words best answer this text, best first.

        Empty when the text carries no words at all. That is not the same as
        "no match", and it must never be: a query that normalizes to nothing
        would otherwise score against everything equally.
        """
        words = tokenize(text)
        if not words or not self._items:
            return []

        scores: dict[int, float] = defaultdict(float)
        for word in words:
            postings = self._postings.get(word)
            if not postings:
                continue
            idf = self._idf(len(postings))
            for position, count in postings:
                scores[position] += idf * self._saturation(count, self._lengths[position])

        ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        return self._best_per_product(ranked, limit)

    def _best_per_product(
        self, ranked: Sequence[tuple[int, float]], limit: int
    ) -> list[Candidate]:
        """One candidate per product, keeping its best-scoring row.

        A row of the sheet is one case of a mapping, not one product: the same
        item can appear under several customers' wordings. Without this, a
        shortlist of five is five ways of writing one boilersuit, and whatever
        chooses next is given one real option instead of five.
        """
        best: dict[str, Candidate] = {}
        for position, score in ranked:
            item = self._items[position]
            key = normalize_code(item.code)
            if key not in best:
                best[key] = Candidate(item=item, score=score)
            if len(best) == limit:
                break
        return list(best.values())

    def shortlist(
        self,
        *,
        code: str | None = None,
        description: str | None = None,
        limit: int = 20,
    ) -> Shortlist:
        """Everything the catalogue can say about one requested line.

        The code's item, when there is one, always leads the list: it is the
        strongest single signal there is. It is not, however, the answer -
        `confirmed` says whether the description search found it too, and a
        caller that ignores that is doing the thing the desk warned about.
        """
        found = self.by_code(code)
        ranked = self.search(description or "", limit=limit) if description else []

        # `None` only when there were no words to check the code against. A
        # description that was searched and did not turn the code's item up has
        # not confirmed it - that is the hard-drive-against-fishing-rod case,
        # and calling it "unknown" would lose exactly the warning worth having.
        searched = bool(description and tokenize(description))
        confirmed = (
            any(candidate.item.code == found.code for candidate in ranked)
            if found is not None and searched
            else None
        )

        candidates = list(ranked)
        if found is not None:
            candidates = [Candidate(item=found, score=_score_of(ranked, found))] + [
                candidate for candidate in ranked if candidate.item.code != found.code
            ]

        return Shortlist(
            candidates=candidates[:limit], by_code=found, confirmed=confirmed
        )

    def _words(self, item: CatalogItem) -> list[str]:
        """What an item is indexed under.

        Our own description and our code, always. How a customer once asked for
        the same product only when that is switched on - it is a second wording
        of one product, which sounds like free recall and measures as the
        opposite while each product is mentioned once.

        The code is in there because customers paste ours into the description
        line as often as they put it in a column of its own.
        """
        words = tokenize(item.description) + [normalize_code(item.code).lower()]
        if self._index_customer_description:
            words += tokenize(item.customer_description)
        return words

    def _idf(self, document_frequency: int) -> float:
        """How much a word narrows things down. A word in every row buys nothing."""
        total = len(self._items)
        return math.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))

    def _saturation(self, count: int, length: int) -> float:
        """BM25's term: saying a word twice is worth less than twice as much,
        and a long description does not win by listing more words."""
        if self._average_length == 0:
            return 0.0
        norm = 1 - B + B * (length / self._average_length)
        return count * (K1 + 1) / (count + K1 * norm)


def _score_of(ranked: Sequence[Candidate], item: CatalogItem) -> float:
    """The description score of an item the code found, or zero for "the words
    did not find it at all" - which is the conflict worth seeing."""
    return next((one.score for one in ranked if one.item.code == item.code), 0.0)


def _column(found: Mapping[str, str], name: str) -> str:
    """One optional column of a row, by its heading. Empty when unset."""
    return (found.get(_heading(name)) or "").strip() if name else ""


def _heading(name: str) -> str:
    """Column headings compared the way people actually retype them."""
    return _NOT_ALPHANUMERIC.sub("", name).lower()
