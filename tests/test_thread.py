"""Thread splitting - the quality gate of the whole pipeline (PLAN.md 8.3).

Assertions check for the presence or absence of meaningful phrases, never for
character offsets or blank-line counts: the corpus was copied through a chat
window, so whitespace is not trustworthy.
"""

import pytest

from src.domain.models import SplitThread
from src.domain.preprocessing.normalize import normalize_text, split_headers
from src.domain.preprocessing.thread import SUSPICIOUS_SPLIT, split_thread
from tests.corpus import load_dataset, load_email_by_id


def split_fixture(email_id: str) -> SplitThread:
    """Run the real L0 -> L1 chain over one fixture."""
    _, body = split_headers(normalize_text(load_email_by_id(email_id)))
    return split_thread(body)


# (fixture id, phrase, must the newest message contain it?)
GATE = [
    ("001", "you may find attached our RFQ", True),
    ("004", "we need 100% Isopropanol", True),
    # The trap: 004 quotes a full RFQ underneath a nine-line reply.
    ("004", "Many thanks for your RFQ", False),
    ("005", "Iso Propyl Alcohol 99.9%", True),
    ("005", "Request For Quote For OASIS", False),
    ("010", "Please find below reply from vessel", True),
    ("010", "ShipServ", False),
]


@pytest.mark.parametrize(
    ("email_id", "phrase", "expected"), GATE, ids=[f"{e}-{p[:24]}" for e, p, _ in GATE]
)
def test_newest_message_holds_the_right_text(email_id: str, phrase: str, expected: bool) -> None:
    assert (phrase in split_fixture(email_id).latest_message) is expected


def test_first_submission_has_no_quoted_history() -> None:
    thread = split_fixture("001")
    assert thread.is_reply is False
    assert thread.quoted_messages == []


def test_deep_reply_keeps_every_quoted_message() -> None:
    thread = split_fixture("004")
    assert thread.is_reply is True
    assert len(thread.quoted_messages) >= 3


def test_single_message_email_is_not_a_reply() -> None:
    assert split_fixture("012").is_reply is False


def test_quoted_messages_carry_their_own_headers() -> None:
    quoted = split_fixture("005").quoted_messages[0]
    assert quoted.from_address
    assert quoted.sent_at
    assert quoted.subject


def test_no_separators_means_everything_is_the_newest_message() -> None:
    body = "Dear Sirs,\nplease quote 10 oil filters."
    thread = split_thread(body)
    assert thread.latest_message == body
    assert thread.is_reply is False


def test_short_newest_message_raises_a_warning() -> None:
    body = "ok\n\nFrom: a@b.com\nSent: Monday\nSubject: RE: quote\nbody of the quoted mail"
    thread = split_thread(body)
    assert thread.warnings == [SUSPICIOUS_SPLIT]
    # The quoted history is still handed over - the caller decides what to do.
    assert thread.quoted_messages


def test_banner_inside_a_quoted_body_is_not_a_boundary() -> None:
    """A CAUTION banner only starts a message when a header block follows it."""
    body = (
        "Dear John,\nprice is AED 100 per can.\n"
        "\nFrom: john.baker@our-company.com\nSent: Monday\nSubject: RE: quote\n"
        "CAUTION: External Sender. Do not click links.\nDear John.\nMSDS attached.\n"
    )
    assert len(split_thread(body).quoted_messages) == 1
