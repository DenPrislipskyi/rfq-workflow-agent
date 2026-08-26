"""Scoring one evaluation run.

Pure functions over a list of `Prediction`, so every formula is unit-tested
without a model: a recall computed the wrong way does not crash, it quietly
reports that everything is fine. A ratio over an empty set returns None, not
0.0 - "no such emails" and "got every one wrong" must not print the same way.
"""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import NamedTuple

# The three categories that mean "a customer wants a price from us".
RFQ_CATEGORIES = frozenset({"NEW_RFQ", "UPDATED_RFQ", "PORTAL_RFQ_NOTIFICATION"})
# The cheapest calls to get right, so PLAN 14.3 holds them to a higher bar.
CHEAP_CATEGORIES = frozenset({"OUTBOUND_OWN", "INTERNAL"})
IGNORE = "IGNORE"


@dataclass(frozen=True, slots=True)
class Prediction:
    """One email's label next to what the agent answered about it."""

    email_id: str
    expected: str
    predicted: str
    recommended_action: str
    needs_human_review: bool
    held_out: bool
    latency_ms: int | None = None

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted


class ClassMetrics(NamedTuple):
    support: int
    predicted: int
    precision: float | None
    recall: float | None
    f1: float | None


def accuracy(predictions: list[Prediction]) -> float | None:
    """Share of emails whose category matches the label."""
    return ratio(sum(item.correct for item in predictions), len(predictions))


def accuracy_over(predictions: list[Prediction], categories: frozenset[str]) -> float | None:
    """Accuracy restricted to emails whose true category is one of `categories`."""
    subset = [item for item in predictions if item.expected in categories]
    return accuracy(subset)


def per_class(predictions: list[Prediction]) -> dict[str, ClassMetrics]:
    """Precision, recall and F1 for every category that appears on either side."""
    labels = {item.expected for item in predictions} | {item.predicted for item in predictions}

    metrics = {}
    for label in sorted(labels):
        hits = sum(item.expected == label == item.predicted for item in predictions)
        support = sum(item.expected == label for item in predictions)
        predicted = sum(item.predicted == label for item in predictions)
        precision = ratio(hits, predicted)
        recall = ratio(hits, support)
        metrics[label] = ClassMetrics(support, predicted, precision, recall, _f1(precision, recall))
    return metrics


def confusion(predictions: list[Prediction]) -> dict[str, Counter[str]]:
    """Expected category -> what was answered instead, and how often."""
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for item in predictions:
        matrix[item.expected][item.predicted] += 1
    return dict(matrix)


def rfq_recall(predictions: list[Prediction]) -> float | None:
    """Of the real RFQs, how many were recognised as some kind of RFQ.

    The three RFQ categories count as one positive class: confusing a new RFQ
    with an updated one still sends it to DST, which is what matters.
    """
    real = [item for item in predictions if item.expected in RFQ_CATEGORIES]
    return ratio(sum(item.predicted in RFQ_CATEGORIES for item in real), len(real))


def escaped_rfqs(predictions: list[Prediction]) -> list[str]:
    """Real RFQs routed to IGNORE with nobody asked to look at them.

    The failure that costs a sale, and the only one the safety net exists to
    prevent; an RFQ misfiled but flagged for review has not escaped.
    """
    return [
        item.email_id
        for item in predictions
        if item.expected in RFQ_CATEGORIES
        and item.recommended_action == IGNORE
        and not item.needs_human_review
    ]


def escaped_rfq_rate(predictions: list[Prediction]) -> float | None:
    real = sum(item.expected in RFQ_CATEGORIES for item in predictions)
    return ratio(len(escaped_rfqs(predictions)), real)


def p95_latency_ms(predictions: list[Prediction]) -> int | None:
    """Nearest-rank p95. On a corpus this small it is effectively the maximum."""
    latencies = sorted(item.latency_ms for item in predictions if item.latency_ms is not None)
    if not latencies:
        return None
    return latencies[math.ceil(0.95 * len(latencies)) - 1]


def stability(runs: list[list[Prediction]]) -> tuple[float | None, list[str]]:
    """Share of emails answered identically across every pass, and the ones that drifted."""
    if len(runs) < 2:
        return None, []

    answers: dict[str, set[str]] = defaultdict(set)
    for run in runs:
        for item in run:
            answers[item.email_id].add(item.predicted)

    drifted = sorted(email_id for email_id, seen in answers.items() if len(seen) > 1)
    return ratio(len(answers) - len(drifted), len(answers)), drifted


def ratio(part: int, whole: int) -> float | None:
    """None when the question does not apply - a 0.0 would read as total failure."""
    return part / whole if whole else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall == 0:
        return None
    return 2 * precision * recall / (precision + recall)
