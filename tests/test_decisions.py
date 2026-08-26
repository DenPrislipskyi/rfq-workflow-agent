"""The decision journal.

What matters here is not the happy path: it is that a customer's email body
never lands on disk unless someone asked for it, and that one decision is
always exactly one parseable line.
"""

import hashlib
import json
from pathlib import Path

from src.domain.enums import DecisionPath, Direction, EmailCategory, RecommendedAction
from src.domain.models import (
    Attachment,
    ClassificationOutcome,
    ClassificationResult,
    EmailAddress,
    Hints,
    NormalizedEmail,
    Signals,
    SplitThread,
)
from src.infrastructure.storage.decisions import DecisionLog

BODY = "Dear Sir/Madam, you may find attached our RFQ for Engine Materials."


def email() -> NormalizedEmail:
    return NormalizedEmail(
        message_id="AAMkAGI2",
        mailbox="supply@our-company.com",
        sender=EmailAddress(address="purchasing@new-company.com"),
        subject="VSL: NORTH STAR",
        body_text=BODY,
        attachments=[Attachment(filename="E_QUOT_XLS_0015.XLSX")],
    )


def outcome() -> ClassificationOutcome:
    return ClassificationOutcome(
        result=ClassificationResult(
            category=EmailCategory.NEW_RFQ,
            direction=Direction.INBOUND_CUSTOMER,
            requires_action=True,
            is_rfq=True,
            recommended_action=RecommendedAction.FORWARD_TO_DST,
            confidence=0.95,
            needs_human_review=False,
            decision_path=DecisionPath.LLM,
            reasoning="Customer asks the chandler to quote.",
            extracted=Signals(vessel_name="NORTH STAR"),
        ),
        thread=SplitThread(latest_message=BODY),
        hints=Hints(),
        model="gpt-5.6-luna",
        latency_ms=2781,
        input_tokens=6265,
        output_tokens=187,
    )


def log(tmp_path: Path, *, enabled: bool = True, log_bodies: bool = False) -> DecisionLog:
    return DecisionLog(tmp_path / "decisions.jsonl", enabled=enabled, log_bodies=log_bodies)


async def record(journal: DecisionLog) -> str | None:
    return await journal.record(
        source="http", email=email(), outcome=outcome(), prompt_version="v1.0.0"
    )


# --------------------------------------------------------------------------- #
# Confidentiality
# --------------------------------------------------------------------------- #


async def test_the_body_is_hashed_not_stored(tmp_path: Path) -> None:
    """The default has to be safe: this is a flat file on someone's disk."""
    journal = log(tmp_path)
    await record(journal)
    stored = next(journal.records())["email"]

    assert stored["body_text"] is None
    assert stored["body_sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
    assert stored["body_chars"] == len(BODY)
    assert BODY not in (tmp_path / "decisions.jsonl").read_text()


async def test_the_flag_hides_the_body_but_not_the_models_quotes(tmp_path: Path) -> None:
    """The flag is not a redaction guarantee, and this pins that down.

    The prompt asks for near-verbatim `evidence`, so fragments of the customer's
    text are journalled whatever `LOG_EMAIL_BODIES` says. That is deliberate: a
    journal without the justification cannot be reviewed.
    """
    journal = log(tmp_path, log_bodies=False)
    quoting = outcome()
    quoting.result.evidence = ["'you may find attached our RFQ'"]
    await journal.record(
        source="http", email=email(), outcome=quoting, prompt_version="v1.0.0"
    )

    on_disk = (tmp_path / "decisions.jsonl").read_text()
    assert "you may find attached our RFQ" in on_disk


async def test_the_body_is_kept_when_it_was_asked_for(tmp_path: Path) -> None:
    journal = log(tmp_path, log_bodies=True)
    await record(journal)
    assert next(journal.records())["email"]["body_text"] == BODY


async def test_the_same_email_always_hashes_the_same(tmp_path: Path) -> None:
    """The hash is what identifies the text behind a line, so it has to be stable."""
    journal = log(tmp_path)
    await record(journal)
    await record(journal)
    hashes = {entry["email"]["body_sha256"] for entry in journal.records()}
    assert len(hashes) == 1


# --------------------------------------------------------------------------- #
# What a decision line holds
# --------------------------------------------------------------------------- #


async def test_a_decision_line_holds_the_verdict_and_what_it_cost(tmp_path: Path) -> None:
    journal = log(tmp_path)
    decision_id = await record(journal)
    entry = next(journal.records())

    assert entry["decision_id"] == decision_id
    assert entry["source"] == "http"
    assert entry["result"]["category"] == "NEW_RFQ"
    assert entry["result"]["extracted"]["vessel_name"] == "NORTH STAR"
    assert entry["meta"] == {
        "prompt_version": "v1.0.0",
        "model": "gpt-5.6-luna",
        "latency_ms": 2781,
        "input_tokens": 6265,
        "output_tokens": 187,
    }


async def test_every_decision_gets_its_own_id(tmp_path: Path) -> None:
    journal = log(tmp_path)
    ids = {await record(journal), await record(journal)}
    assert len(ids) == 2


async def test_one_decision_is_one_line(tmp_path: Path) -> None:
    """A reader that splits on newlines is the whole point of JSONL."""
    journal = log(tmp_path)
    await record(journal)
    await record(journal)
    lines = (tmp_path / "decisions.jsonl").read_text().strip().split("\n")

    assert len(lines) == 2
    assert all(json.loads(line) for line in lines)


# --------------------------------------------------------------------------- #
# Switched off
# --------------------------------------------------------------------------- #


async def test_nothing_is_written_when_journalling_is_off(tmp_path: Path) -> None:
    """A null id is honest: there is no line on disk to point at."""
    journal = log(tmp_path, enabled=False)

    assert await record(journal) is None
    assert not (tmp_path / "decisions.jsonl").exists()


def test_reading_a_journal_that_does_not_exist_yet_is_empty(tmp_path: Path) -> None:
    assert list(log(tmp_path).records()) == []
