"""The corpus and its labels must stay in sync with the taxonomy.

These tests need no LLM and no network. They catch two kinds of drift early:
a label that no longer exists in the enums, and a fixture file that moved away.
"""

import pytest

from src.domain.enums import Direction, EmailCategory, RecommendedAction
from tests.corpus import load_dataset, load_email

DATASET = load_dataset()
IDS = [row["id"] for row in DATASET]

# Categories that mean "a new or updated RFQ reaches DST".
RFQ_CATEGORIES = {
    EmailCategory.NEW_RFQ,
    EmailCategory.UPDATED_RFQ,
    EmailCategory.PORTAL_RFQ_NOTIFICATION,
}


def test_dataset_is_not_empty() -> None:
    assert DATASET


def test_ids_are_unique() -> None:
    assert len(IDS) == len(set(IDS))


@pytest.mark.parametrize("row", DATASET, ids=IDS)
def test_fixture_exists_and_has_content(row: dict) -> None:
    assert load_email(row["fixture"]).strip()


@pytest.mark.parametrize("row", DATASET, ids=IDS)
def test_labels_are_valid_enum_members(row: dict) -> None:
    EmailCategory(row["expected_category"])
    Direction(row["expected_direction"])
    RecommendedAction(row["expected_action"])


@pytest.mark.parametrize("row", DATASET, ids=IDS)
def test_is_rfq_follows_from_the_category(row: dict) -> None:
    """L4 derives is_rfq from the category, so the labels must already agree."""
    expected = EmailCategory(row["expected_category"]) in RFQ_CATEGORIES
    assert row["expected_is_rfq"] is expected


@pytest.mark.parametrize("row", DATASET, ids=IDS)
def test_requires_action_follows_from_the_action(row: dict) -> None:
    """Anything routed somewhere requires action; only IGNORE does not."""
    expected = row["expected_action"] != RecommendedAction.IGNORE
    assert row["expected_requires_action"] is expected
