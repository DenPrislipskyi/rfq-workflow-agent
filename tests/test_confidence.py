"""The confidence formula: facts about a candidate in, a number out.

Every case here is a line the desk has seen. The formula is the part of
confidence that must never surprise anybody, so each test names the reading a
person would give the same facts.
"""

from src.services.matching.confidence import (
    Agreement,
    Assessment,
    Attribute,
    confidences,
)

MATCH, CONFLICT, UNSTATED, UNCONFIRMED = (
    Agreement.MATCH,
    Agreement.CONFLICT,
    Agreement.UNSTATED,
    Agreement.UNCONFIRMED,
)


def size(agreement: Agreement, offered: str = "") -> Attribute:
    return Attribute(name="size", key=True, agreement=agreement, offered=offered)


def brand(agreement: Agreement, offered: str = "") -> Attribute:
    return Attribute(name="brand", key=False, agreement=agreement, offered=offered)


def same(*attributes: Attribute) -> Assessment:
    return Assessment(same_product=True, attributes=attributes)


def test_four_sizes_the_line_did_not_name_are_a_guess_among_four():
    """`Steel toe sneakers` against 25, 26, 27 and 29 cm: 50 + 50 / 4 = 62.5,
    rounded half up to 63 - not Python's banker's 62."""
    sneakers = [same(size(UNSTATED, value)) for value in ("25CM", "26 CM", "27 CM", "29 CM")]

    assert confidences(sneakers) == [63, 63, 63, 63]


def test_the_size_that_was_named_is_certain_and_the_others_are_ruled_out():
    """`Steel toe sneakers 25 cm`: the 25 is it, the 29 contradicts the line."""
    assert confidences([same(size(MATCH, "25CM")), same(size(CONFLICT, "29 CM"))]) == [100, 0]


def test_a_size_nobody_named_on_a_product_that_comes_in_one_size_is_no_guess():
    """`Kraft cheese slices` against `CHEESE, SLICED 200 GRM KRAFT`, alone."""
    assert confidences([same(size(UNSTATED, "200 GRM"))]) == [100]


def test_one_value_written_two_ways_is_one_variant():
    assert confidences([same(size(UNSTATED, "25CM")), same(size(UNSTATED, "25 cm"))]) == [100, 100]


def test_every_property_agreeing_is_certain():
    """The M14 x 50 bolt: 56% by word count, because `hexagon` is not `hex`."""
    bolt = same(size(MATCH, "M14 X 50MM"), Attribute("length", True, MATCH, "50MM"))

    assert confidences([bolt]) == [100]


def test_the_same_product_with_nothing_to_tell_variants_apart_is_certain():
    assert confidences([same()]) == [100]


def test_a_brand_the_line_named_and_the_product_does_not_costs_a_little():
    """The Acdelco battery: 12 V and 200 Ah agree, the brand is unconfirmed.
    50 + 50 x (3 + 3 + 0.5) / 7 = 96.4."""
    battery = same(
        Attribute("voltage", True, MATCH, "12v"),
        Attribute("capacity", True, MATCH, "200AH"),
        brand(UNCONFIRMED),
    )

    assert confidences([battery]) == [96]


def test_a_key_property_left_open_costs_more_than_a_minor_one():
    key_open = same(size(UNSTATED, "25CM"), brand(MATCH, "Lipton"))
    minor_open = same(size(MATCH, "25CM"), brand(UNSTATED, "Lipton"))
    other_key = same(size(UNSTATED, "29CM"), brand(MATCH, "Lipton"))
    other_minor = same(size(MATCH, "25CM"), brand(UNSTATED, "Unilever"))

    key_score, _ = confidences([key_open, other_key])
    minor_score, _ = confidences([minor_open, other_minor])

    assert key_score < minor_score


def test_a_conflicting_property_rules_the_candidate_out():
    """`Ice tea green` against `ICE TEA, LEMON`: the flavours disagree."""
    lemon = same(Attribute("flavour", True, CONFLICT, "LEMON"))

    assert confidences([lemon]) == [0]


def test_a_different_product_scores_nothing():
    goggles = Assessment(same_product=False, attributes=(size(MATCH, "L"),))

    assert confidences([goggles]) == [0]


def test_a_candidate_ruled_out_is_not_one_of_the_choices():
    """Two sneakers left open on size, and a third that is a different product:
    a guess between two, not among three."""
    left = [
        same(size(UNSTATED, "25CM")),
        same(size(UNSTATED, "29CM")),
        Assessment(same_product=False, attributes=(size(UNSTATED, "27CM"),)),
    ]

    assert confidences(left) == [75, 75, 0]


def test_properties_are_counted_by_name_whatever_the_case():
    left = [
        same(Attribute("Size", True, UNSTATED, "25CM")),
        same(Attribute("size", True, UNSTATED, "29CM")),
    ]

    assert confidences(left) == [75, 75]


def test_no_candidates_no_scores():
    assert confidences([]) == []
