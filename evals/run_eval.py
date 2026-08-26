"""Run the classifier over the labelled corpus and score it.

    uv run python -m evals.run_eval --mailbox supply@example.com
    uv run python -m evals.run_eval --provider anthropic --model claude-sonnet-5
    uv run python -m evals.run_eval --repeat 3

Exits non-zero when an acceptance criterion from PLAN 14.3 fails, so it can gate
rather than inform. Ten of the thirteen corpus emails are few-shot examples in
the prompt, so only the three-email "held out" block measures generalisation.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.config import Settings, get_settings
from src.core.lifespan import build_llm
from src.domain.preprocessing.raw_email import parse_raw_email
from src.domain.rules.registries import Registries
from src.services.classification.few_shots import FEW_SHOTS
from src.services.classification.pipeline import ClassificationPipeline
from tests.corpus import load_dataset, load_email

from evals import metrics
from evals.metrics import CHEAP_CATEGORIES, Prediction

REPORTS_DIR = Path("evals/reports")
# Low enough not to trip provider rate limits.
DEFAULT_CONCURRENCY = 5
IN_PROMPT = {shot.source_id for shot in FEW_SHOTS}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", help="override LLM_PROVIDER")
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--extra-options", help="override LLM_EXTRA_OPTIONS, as JSON")
    parser.add_argument("--repeat", type=int, default=1, help="passes, for stability")
    parser.add_argument("--limit", type=int, help="score only the first N emails")
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="fixtures in flight"
    )
    parser.add_argument(
        "--mailbox",
        help="the mailbox the corpus was collected from, e.g. supply@example.com. "
        "Defaults to MAILBOX_ADDRESS, which is usually a different company - and then "
        "the model cannot tell which sender is 'us', so scores read low for the wrong reason.",
    )
    parser.add_argument("--no-report", action="store_true", help="print only, write nothing")
    return parser.parse_args()


def build_settings(args: argparse.Namespace) -> Settings:
    """Command-line flags win over the environment; nothing else changes."""
    overrides: dict[str, Any] = {}
    if args.provider:
        overrides["LLM_PROVIDER"] = args.provider
    if args.model:
        overrides["LLM_MODEL"] = args.model
    if args.extra_options:
        overrides["LLM_EXTRA_OPTIONS"] = json.loads(args.extra_options)
    if args.mailbox:
        overrides["MAILBOX_ADDRESS"] = args.mailbox
    return get_settings().model_copy(update=overrides)


async def run_pass(
    pipeline: ClassificationPipeline, rows: list[dict], concurrency: int
) -> list[Prediction]:
    """Classify every fixture once, a few at a time."""
    limit = asyncio.Semaphore(concurrency)

    async def one(row: dict) -> Prediction:
        async with limit:
            outcome = await pipeline.classify(parse_raw_email(load_email(row["fixture"])))
        result = outcome.result
        return Prediction(
            email_id=row["id"],
            expected=row["expected_category"],
            predicted=result.category.value,
            recommended_action=result.recommended_action.value,
            needs_human_review=result.needs_human_review,
            held_out=row["id"] not in IN_PROMPT,
            latency_ms=outcome.latency_ms,
        )

    return list(await asyncio.gather(*(one(row) for row in rows)))


def acceptance(predictions: list[Prediction], stable: float | None) -> list[dict[str, Any]]:
    """The v1 gate from PLAN 14.3. A criterion with nothing to measure is skipped."""
    checks = [
        ("escaped_rfq_rate == 0", metrics.escaped_rfq_rate(predictions), lambda v: v == 0),
        ("RFQ recall >= 0.95", metrics.rfq_recall(predictions), lambda v: v >= 0.95),
        ("accuracy >= 0.85", metrics.accuracy(predictions), lambda v: v >= 0.85),
        (
            "accuracy on OUTBOUND_OWN + INTERNAL >= 0.95",
            metrics.accuracy_over(predictions, CHEAP_CATEGORIES),
            lambda v: v >= 0.95,
        ),
        ("p95 latency <= 5000 ms", metrics.p95_latency_ms(predictions), lambda v: v <= 5000),
        ("same category on every pass", stable, lambda v: v == 1.0),
    ]
    return [
        {"criterion": name, "value": value, "passed": passes(value)}
        for name, value, passes in checks
        if value is not None
    ]


def build_report(
    runs: list[list[Prediction]], settings: Settings, elapsed_s: float
) -> dict[str, Any]:
    predictions = runs[0]
    stable, drifted = metrics.stability(runs)
    held_out = [item for item in predictions if item.held_out]

    return {
        "run": {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "provider": settings.LLM_PROVIDER,
            "model": settings.LLM_MODEL,
            "extra_options": settings.LLM_EXTRA_OPTIONS,
            "prompt_version": settings.PROMPT_VERSION,
            "mailbox_domain": settings.MAILBOX_ADDRESS.rsplit("@", 1)[-1],
            "fast_path_enabled": settings.FAST_PATH_ENABLED,
            "emails": len(predictions),
            "passes": len(runs),
            "elapsed_s": round(elapsed_s, 1),
        },
        "headline": {
            "accuracy": metrics.accuracy(predictions),
            "rfq_recall": metrics.rfq_recall(predictions),
            "escaped_rfq_rate": metrics.escaped_rfq_rate(predictions),
            "escaped_rfqs": metrics.escaped_rfqs(predictions),
            "p95_latency_ms": metrics.p95_latency_ms(predictions),
            "stability": stable,
            "drifted": drifted,
        },
        "held_out": {
            "ids": [item.email_id for item in held_out],
            "accuracy": metrics.accuracy(held_out),
            "note": "the other emails are few-shot examples; their scores are contaminated",
        },
        "per_class": {
            label: value._asdict() for label, value in metrics.per_class(predictions).items()
        },
        "confusion": {
            expected: dict(counts) for expected, counts in metrics.confusion(predictions).items()
        },
        "acceptance": acceptance(predictions, stable),
        "predictions": [
            {
                "id": item.email_id,
                "expected": item.expected,
                "predicted": item.predicted,
                "correct": item.correct,
                "held_out": item.held_out,
                "recommended_action": item.recommended_action,
                "needs_human_review": item.needs_human_review,
                "latency_ms": item.latency_ms,
            }
            for item in predictions
        ],
    }


def save_report(report: dict[str, Any], settings: Settings) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{stamp}_{settings.PROMPT_VERSION}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def show(report: dict[str, Any]) -> None:
    run, headline = report["run"], report["headline"]
    print(
        f"\n{run['model']} via {run['provider']} · prompt {run['prompt_version']} · "
        f"{run['emails']} emails · {run['passes']} pass(es) · {run['elapsed_s']}s"
    )
    if run["extra_options"]:
        print(f"options: {run['extra_options']}")

    print("\n  id   expected                    predicted                    ms   ")
    for item in report["predictions"]:
        mark = " " if item["correct"] else "x"
        held = "*" if item["held_out"] else " "
        print(
            f"{mark} {item['id']:<4}{held}{item['expected']:<28}{item['predicted']:<28}"
            f"{item['latency_ms'] or '-':>6}"
        )
    print("  (* = held out, not used as a few-shot example)")

    print("\nHeadline")
    _line("accuracy", headline["accuracy"])
    _line("RFQ recall", headline["rfq_recall"])
    _line("escaped RFQ rate", headline["escaped_rfq_rate"], note="the metric that matters")
    if headline["escaped_rfqs"]:
        print(f"  escaped: {', '.join(headline['escaped_rfqs'])}")
    print(f"  {'p95 latency':<34}{headline['p95_latency_ms']} ms")
    if headline["stability"] is not None:
        _line("stability across passes", headline["stability"])
        if headline["drifted"]:
            print(f"  drifted: {', '.join(headline['drifted'])}")

    held = report["held_out"]
    print(f"\nHeld out ({', '.join(held['ids'])})")
    _line("accuracy", held["accuracy"])
    print(f"  {held['note']}")

    print("\nPer class")
    print(f"  {'category':<28}{'support':>8}{'pred':>6}{'P':>8}{'R':>8}{'F1':>8}")
    for label, value in report["per_class"].items():
        print(
            f"  {label:<28}{value['support']:>8}{value['predicted']:>6}"
            f"{_num(value['precision']):>8}{_num(value['recall']):>8}{_num(value['f1']):>8}"
        )

    print("\nConfusion (expected -> answered)")
    for expected, counts in report["confusion"].items():
        answers = ", ".join(f"{name} {count}" for name, count in counts.items())
        print(f"  {expected:<28}{answers}")

    print("\nAcceptance (PLAN 14.3)")
    for check in report["acceptance"]:
        print(f"  {'PASS' if check['passed'] else 'FAIL'}  {check['criterion']:<44}{_num(check['value'])}")


def _line(label: str, value: float | None, note: str = "") -> None:
    suffix = f"   <- {note}" if note else ""
    print(f"  {label:<34}{_num(value)}{suffix}")


def _num(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


async def main() -> int:
    args = parse_args()
    settings = build_settings(args)
    pipeline = ClassificationPipeline(
        llm=build_llm(settings),
        registries=Registries.load(settings.REGISTRIES_PATH, mailbox=settings.MAILBOX_ADDRESS),
        settings=settings,
    )

    rows = load_dataset()[: args.limit]
    started = asyncio.get_running_loop().time()
    runs = [await run_pass(pipeline, rows, args.concurrency) for _ in range(args.repeat)]
    elapsed = asyncio.get_running_loop().time() - started

    report = build_report(runs, settings, elapsed)
    show(report)

    if not args.no_report:
        print(f"\nReport: {save_report(report, settings)}")

    failed = [check for check in report["acceptance"] if not check["passed"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
