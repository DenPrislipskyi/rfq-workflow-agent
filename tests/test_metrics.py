"""Eval metrics.

These formulas decide whether the agent is judged good enough to ship, and a
wrong one does not crash - it reports success. So each is checked against a
hand-counted example, including the cases where the answer is "not applicable".
"""

from evals.metrics import (
    CHEAP_CATEGORIES,
    Prediction,
    accuracy,
    accuracy_over,
    confusion,
    escaped_rfq_rate,
    escaped_rfqs,
    p95_latency_ms,
    per_class,
    rfq_recall,
    stability,
)


def prediction(
    email_id: str,
    expected: str,
    predicted: str | None = None,
    *,
    action: str = "FORWARD_TO_DST",
    review: bool = False,
    held_out: bool = False,
    latency_ms: int | None = None,
) -> Prediction:
    return Prediction(
        email_id=email_id,
        expected=expected,
        predicted=predicted or expected,
        recommended_action=action,
        needs_human_review=review,
        held_out=held_out,
        latency_ms=latency_ms,
    )


# --------------------------------------------------------------------------- #
# Accuracy
# --------------------------------------------------------------------------- #


def test_accuracy_counts_exact_category_matches() -> None:
    predictions = [
        prediction("1", "NEW_RFQ"),
        prediction("2", "INTERNAL"),
        prediction("3", "SUPPLIER_CORRESPONDENCE", "OUTBOUND_OWN"),
    ]
    assert accuracy(predictions) == 2 / 3


def test_accuracy_over_a_subset_ignores_everything_else() -> None:
    """PLAN 14.3 holds OUTBOUND_OWN and INTERNAL to a higher bar than the rest."""
    predictions = [
        prediction("1", "OUTBOUND_OWN"),
        prediction("2", "INTERNAL"),
        prediction("3", "NEW_RFQ", "CUSTOMER_CLARIFICATION"),
    ]
    assert accuracy_over(predictions, CHEAP_CATEGORIES) == 1.0


def test_an_empty_set_is_not_applicable_rather_than_zero() -> None:
    """0.0 would read as "got everything wrong" instead of "nothing to measure"."""
    assert accuracy([]) is None
    assert accuracy_over([prediction("1", "NEW_RFQ")], CHEAP_CATEGORIES) is None


# --------------------------------------------------------------------------- #
# Per class and confusion
# --------------------------------------------------------------------------- #


def test_per_class_precision_and_recall_are_counted_separately() -> None:
    predictions = [
        prediction("1", "NEW_RFQ"),
        prediction("2", "NEW_RFQ", "UPDATED_RFQ"),
        prediction("3", "INTERNAL", "NEW_RFQ"),
    ]
    new_rfq = per_class(predictions)["NEW_RFQ"]

    assert new_rfq.support == 2
    assert new_rfq.predicted == 2
    assert new_rfq.precision == 0.5
    assert new_rfq.recall == 0.5
    assert new_rfq.f1 == 0.5


def test_a_category_never_predicted_has_no_precision_but_still_has_recall() -> None:
    predictions = [prediction("1", "SPAM_MARKETING", "OTHER_NON_ACTIONABLE")]
    spam = per_class(predictions)["SPAM_MARKETING"]

    assert spam.support == 1
    assert spam.predicted == 0
    assert spam.precision is None
    assert spam.recall == 0.0
    assert spam.f1 is None


def test_confusion_shows_what_was_answered_instead() -> None:
    predictions = [
        prediction("1", "SUPPLIER_CORRESPONDENCE"),
        prediction("2", "SUPPLIER_CORRESPONDENCE", "OUTBOUND_OWN"),
    ]
    assert dict(confusion(predictions)["SUPPLIER_CORRESPONDENCE"]) == {
        "SUPPLIER_CORRESPONDENCE": 1,
        "OUTBOUND_OWN": 1,
    }


# --------------------------------------------------------------------------- #
# The RFQ metrics that decide whether this ships
# --------------------------------------------------------------------------- #


def test_the_three_rfq_categories_count_as_one_positive_class() -> None:
    """Calling a new RFQ an updated one still sends it to DST, which is the point."""
    predictions = [
        prediction("1", "NEW_RFQ", "UPDATED_RFQ"),
        prediction("2", "PORTAL_RFQ_NOTIFICATION"),
    ]
    assert rfq_recall(predictions) == 1.0


def test_an_rfq_read_as_something_else_lowers_recall() -> None:
    predictions = [
        prediction("1", "NEW_RFQ", "CUSTOMER_CLARIFICATION"),
        prediction("2", "NEW_RFQ"),
    ]
    assert rfq_recall(predictions) == 0.5


def test_an_rfq_sent_to_ignore_unflagged_has_escaped() -> None:
    predictions = [
        prediction("1", "NEW_RFQ", "OUTBOUND_OWN", action="IGNORE", review=False),
        prediction("2", "NEW_RFQ"),
    ]
    assert escaped_rfqs(predictions) == ["1"]
    assert escaped_rfq_rate(predictions) == 0.5


def test_an_rfq_flagged_for_review_has_not_escaped() -> None:
    """This is exactly what the safety net buys: wrong, but not lost."""
    predictions = [prediction("1", "NEW_RFQ", "OUTBOUND_OWN", action="IGNORE", review=True)]

    assert escaped_rfqs(predictions) == []
    assert escaped_rfq_rate(predictions) == 0.0


def test_a_misfiled_rfq_that_still_goes_to_a_human_has_not_escaped() -> None:
    predictions = [prediction("1", "NEW_RFQ", "UNCERTAIN", action="HUMAN_REVIEW", review=True)]
    assert escaped_rfq_rate(predictions) == 0.0


def test_the_rfq_metrics_are_not_applicable_without_rfqs() -> None:
    predictions = [prediction("1", "INTERNAL", action="IGNORE")]

    assert rfq_recall(predictions) is None
    assert escaped_rfq_rate(predictions) is None


# --------------------------------------------------------------------------- #
# Latency and stability
# --------------------------------------------------------------------------- #


def test_p95_takes_the_nearest_rank_and_skips_rule_decisions() -> None:
    """A fast-path decision has no latency to report - it never called a model."""
    predictions = [prediction(str(i), "NEW_RFQ", latency_ms=i * 1000) for i in range(1, 21)]
    predictions.append(prediction("rule", "INTERNAL", latency_ms=None))

    assert p95_latency_ms(predictions) == 19000


def test_p95_is_not_applicable_when_no_model_was_called() -> None:
    assert p95_latency_ms([prediction("1", "INTERNAL")]) is None


def test_stability_needs_more_than_one_pass_to_mean_anything() -> None:
    assert stability([[prediction("1", "NEW_RFQ")]]) == (None, [])


def test_stability_names_the_emails_that_drifted() -> None:
    first = [prediction("1", "NEW_RFQ"), prediction("2", "INTERNAL")]
    second = [prediction("1", "NEW_RFQ"), prediction("2", "INTERNAL", "OUTBOUND_OWN")]

    assert stability([first, second]) == (0.5, ["2"])


def test_identical_passes_are_fully_stable() -> None:
    run = [prediction("1", "NEW_RFQ"), prediction("2", "INTERNAL")]
    assert stability([run, run, run]) == (1.0, [])
