"""The classification endpoint, end to end with a fake model.

The app is assembled without its lifespan on purpose: these tests must not open
a Graph subscription or read the developer's `.env`. Everything below the HTTP
layer is real - the request really is parsed, split, hinted and finalized.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import register_exceptions, register_routers
from src.api.dependencies import get_triage
from src.core.config import get_settings
from src.domain.enums import Direction, EmailCategory
from src.domain.rules.registries import Registries
from src.infrastructure.llm.exceptions import LLMCallError, LLMParsingError, LLMTimeoutError
from src.infrastructure.storage.decisions import DecisionLog
from src.services.classification.pipeline import ClassificationPipeline
from src.services.classification.schemas import LLMClassification
from src.services.triage import EmailTriage
from src.domain.preprocessing.raw_email import parse_raw_email
from tests.corpus import load_email_by_id
from tests.fakes import BrokenLLM, FakeLLM, fake_settings

REGISTRIES = Registries.load(
    Path("config/registries.yaml"), mailbox="supply@our-company.com"
)
URL = "/api/v1/emails/classify"

NEW_RFQ_ANSWER = LLMClassification(
    category=EmailCategory.NEW_RFQ,
    direction=Direction.INBOUND_CUSTOMER,
    is_rfq=True,
    confidence=0.95,
    reasoning="Customer asks the chandler to quote an attached RFQ form.",
    evidence=["'find attached our RFQ'"],
)


def client(llm=None, journal: DecisionLog | None = None, **settings_overrides) -> TestClient:
    """An app with a real pipeline behind a fake model.

    Journalling is off unless a test asks for it, so the ordinary contract tests
    never touch the filesystem.
    """
    settings = fake_settings(**settings_overrides)
    pipeline = ClassificationPipeline(llm or FakeLLM(NEW_RFQ_ANSWER), REGISTRIES, settings)
    decisions = journal or DecisionLog(Path("unused.jsonl"), enabled=False, log_bodies=False)
    triage = EmailTriage(pipeline, decisions, settings.PROMPT_VERSION)

    app = FastAPI()
    register_exceptions(app)
    register_routers(app)
    app.dependency_overrides[get_triage] = lambda: triage
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


STRUCTURED = {
    "message_id": "AAMkAGI2",
    "mailbox": "supply@our-company.com",
    "region_hint": "UAE",
    "sender": {"name": "NEW COMPANY", "address": "purchasing@new-company.com"},
    "to": [{"address": "supply@our-company.com"}],
    "subject": "VSL: NORTH STAR, QUOTATION: 0015-AB000001C",
    "body_text": "Dear Sir/Madam, you may find attached our RFQ. Quotation due date is 02/June/2026.",
    "attachments": [{"filename": "E_QUOT_XLS_0015.XLSX", "size_bytes": 40311}],
}


# --------------------------------------------------------------------------- #
# The two input modes
# --------------------------------------------------------------------------- #


def test_the_structured_mode_returns_the_full_contract() -> None:
    body = client().post(URL, json=STRUCTURED).json()

    assert body["schema_version"] == "1.0"
    assert body["message_id"] == "AAMkAGI2"
    assert body["category"] == "NEW_RFQ"
    assert body["recommended_action"] == "FORWARD_TO_DST"
    assert body["requires_action"] is True
    assert body["is_rfq"] is True
    assert body["needs_human_review"] is False
    assert body["decision_path"] == "LLM"
    assert body["meta"]["prompt_version"] == "v1.0.0"
    assert body["meta"]["model"] == "fake"


def test_the_raw_mode_recovers_the_header_from_the_dump() -> None:
    body = client().post(URL, json={"raw_text": load_email_by_id("001")}).json()

    sender = parse_raw_email(load_email_by_id("001")).sender
    assert sender is not None
    assert body["extracted"]["customer_domain"] == sender.domain
    assert body["extracted"]["quote_due_date"] == "02/June/2026"
    assert body["extracted"]["attachment_kinds"] == ["RFQ_FORM_PDF", "RFQ_FORM_XLSX"]
    assert "received_at_not_parsed" in body["parse_warnings"]


def test_an_html_only_body_is_converted_before_classification() -> None:
    request = {
        "subject": "RFQ for MV Test",
        "body_html": "<div>Dear Sir<br>please quote the attached list</div>",
    }
    body = client().post(URL, json=request).json()
    assert body["category"] == "NEW_RFQ"
    assert body["thread"]["latest_message_chars"] > 0


def test_the_thread_block_describes_the_split() -> None:
    body = client().post(URL, json={"raw_text": load_email_by_id("004")}).json()

    assert body["thread"]["is_reply"] is True
    assert body["thread"]["quoted_messages_count"] == 6


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


INVALID = [
    ("nothing at all", {}),
    ("only a subject", {"subject": "VSL: NORTH STAR"}),
    ("only a body", {"body_text": "please quote"}),
    ("blank raw text", {"raw_text": "   "}),
]


@pytest.mark.parametrize(("name", "payload"), INVALID, ids=[n for n, _ in INVALID])
def test_an_incomplete_request_is_rejected(name: str, payload: dict) -> None:
    assert client().post(URL, json=payload).status_code == 422


def test_a_subject_with_either_body_part_is_enough() -> None:
    assert client().post(URL, json={"subject": "S", "body_text": "b"}).status_code == 200
    assert client().post(URL, json={"subject": "S", "body_html": "<p>b</p>"}).status_code == 200


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


FAILURES = [
    ("timeout", LLMTimeoutError("fake-model", 30.0), 504),
    ("provider refused", LLMCallError("fake-model", RuntimeError("boom")), 502),
    ("unparsable answer", LLMParsingError("LLMClassification", "not json"), 502),
]


@pytest.mark.parametrize(
    ("name", "error", "expected"), FAILURES, ids=[n for n, _, _ in FAILURES]
)
def test_model_failures_map_to_their_own_status(name: str, error, expected: int) -> None:
    response = client(llm=BrokenLLM(error)).post(URL, json=STRUCTURED)

    assert response.status_code == expected
    assert response.json()["message"]


# --------------------------------------------------------------------------- #
# The fast path over HTTP
# --------------------------------------------------------------------------- #


def test_a_rule_decision_reports_no_model_in_meta() -> None:
    internal = {
        "sender": {"address": "robert.hall@our-company.com"},
        "to": [{"address": "michael.reed@our-company.com"}],
        "subject": "RFQ inserted",
        "body_text": "Prices has been submitted in the CUSTOMER PORTAL.",
    }
    body = client().post(URL, json=internal).json()

    assert body["category"] == "INTERNAL"
    assert body["decision_path"] == "RULES_FAST_PATH"
    assert body["recommended_action"] == "IGNORE"
    assert body["meta"]["model"] is None
    assert body["meta"]["latency_ms"] is None


# --------------------------------------------------------------------------- #
# Journalling
# --------------------------------------------------------------------------- #


def test_no_decision_id_when_journalling_is_off() -> None:
    """A null id is honest: nothing was written, so there is no line to point at."""
    assert client().post(URL, json=STRUCTURED).json()["decision_id"] is None


def test_a_classified_email_is_journalled_and_gets_an_id(tmp_path: Path) -> None:
    journal = DecisionLog(tmp_path / "decisions.jsonl", enabled=True, log_bodies=False)
    body = client(journal=journal).post(URL, json=STRUCTURED).json()

    entries = list(journal.records())
    assert len(entries) == 1
    assert entries[0]["decision_id"] == body["decision_id"]
    assert entries[0]["source"] == "http"
    assert entries[0]["result"]["category"] == body["category"]


