"""Loading the labelled email corpus.

The corpus is the source of truth for both unit tests and the eval harness:
`evals/dataset.jsonl` holds one labelled row per email, and each row points at
a fixture file by a path relative to the project root.
"""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
DATASET_PATH = PROJECT_ROOT / "evals" / "dataset.jsonl"


def load_dataset() -> list[dict]:
    """Read every labelled row from the dataset."""
    lines = DATASET_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_email(fixture: str) -> str:
    """Read one fixture by the path stored in a dataset row."""
    return (PROJECT_ROOT / fixture).read_text(encoding="utf-8")


def load_email_by_id(email_id: str) -> str:
    """Read one fixture by its dataset id, e.g. "004"."""
    for row in load_dataset():
        if row["id"] == email_id:
            return load_email(row["fixture"])
    raise KeyError(f"no email with id {email_id!r} in {DATASET_PATH}")
