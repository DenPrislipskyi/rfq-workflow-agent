"""The log, as the thing a live run is read out of afterwards.

Every test here is about a way the log has failed before or would fail
silently: lines that cannot be attributed to an email, a library that buries
them, a context variable that does not survive `asyncio.gather`, and a format
field that raises at write time instead of printing.
"""

import asyncio
import io
import logging

import pytest

from src.core.logging import (
    HEALTH_PATH,
    NOISY_LIBRARIES,
    Tally,
    also_the_decision,
    configure_logging,
    for_one_email,
    tally,
)


@pytest.fixture
def log() -> io.StringIO:
    """The real handler, formatter and filters, writing where we can read them.

    Global logging state is saved and put back: a test that left `pdfminer`
    muted, or the root logger at DEBUG, would quietly change every test after
    it.
    """
    root = logging.getLogger()
    watched = [*NOISY_LIBRARIES, "uvicorn.access"]
    before = (root.handlers[:], root.level, {name: logging.getLogger(name).level for name in watched})
    filters = {name: logging.getLogger(name).filters[:] for name in watched}

    configure_logging("DEBUG")
    written = io.StringIO()
    root.handlers[0].setStream(written)  # ty: ignore

    yield written

    handlers, level, levels = before
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, saved in levels.items():
        logging.getLogger(name).setLevel(saved)
        logging.getLogger(name).filters[:] = filters[name]


# --- which email a line belongs to ----------------------------------------


def test_a_line_logged_while_handling_an_email_names_it(log: io.StringIO) -> None:
    """Two emails arriving together interleave - the pipeline awaits network on
    every step - so a line that cannot name its email is a line nobody can use."""
    with for_one_email("AAMkAGNjYzI0NDU4"):
        logging.getLogger("src.services.handlers").info("Handling")

    assert "jYzI0NDU4" in log.getvalue()


def test_a_line_logged_outside_one_says_so_rather_than_borrowing_the_last(
    log: io.StringIO,
) -> None:
    with for_one_email("AAMk-first"):
        logging.getLogger("src").info("inside")
    logging.getLogger("src").info("outside")

    inside, outside = log.getvalue().splitlines()
    assert "AAMk-first" in inside
    assert "AAMk-first" not in outside
    assert "| - " in outside


def test_the_journals_id_joins_the_tag_once_there_is_one(log: io.StringIO) -> None:
    """The two ids answer different questions: one is what Graph calls this
    email, the other names the journal line and the workbook on disk."""
    with for_one_email("AAMk-1"):
        logging.getLogger("src").info("before")
        also_the_decision("5bdbffcb-8015-4f0a-b744-f5a362908ba1")
        logging.getLogger("src").info("after")

    before, after = log.getvalue().splitlines()
    assert "AAMk-1" in before and "5bdbffcb" not in before
    assert "AAMk-1/5bdbffcb" in after


def test_the_tag_does_not_leak_out_of_the_email_it_belongs_to(log: io.StringIO) -> None:
    with for_one_email("AAMk-1"):
        also_the_decision("dddddddd-0000")
    with for_one_email("AAMk-2"):
        logging.getLogger("src").info("second email")

    assert "dddddddd" not in log.getvalue()


async def test_a_line_from_inside_a_gather_still_names_the_email(log: io.StringIO) -> None:
    """Attachments are read concurrently, and a task gets a copy of the context.
    The tag is read, not written, in there - so the copy is enough."""

    async def read(name: str) -> None:
        logging.getLogger("src.services.extraction.file_reader").info("read %s", name)

    with for_one_email("AAMk-1"):
        await asyncio.gather(read("a.xlsx"), read("b.pdf"))

    lines = log.getvalue().splitlines()
    assert len(lines) == 2
    assert all("AAMk-1" in line for line in lines)


def test_a_library_line_is_formatted_rather_than_raising(log: io.StringIO) -> None:
    """The format asks every record for an email column. A record from code
    that never heard of one must print, not blow up at write time."""
    logging.getLogger("httpx").warning("HTTP Request: POST https://graph 200 OK")

    assert "200 OK" in log.getvalue()


# --- what is kept out of it -----------------------------------------------


def test_the_libraries_stay_quiet_even_when_our_own_level_is_debug(
    log: io.StringIO,
) -> None:
    """DEBUG is turned on to read our lines. `pdfminer` logs several per page
    and would make that impossible."""
    logging.getLogger("pdfminer").debug("resolving font")
    logging.getLogger("httpx").info("HTTP Request: POST https://api 200 OK")
    logging.getLogger("src.services.handlers").debug("ours")

    assert "resolving font" not in log.getvalue()
    assert "HTTP Request" not in log.getvalue()
    assert "ours" in log.getvalue()


def test_a_raised_library_level_is_honoured(log: io.StringIO) -> None:
    """The muting is a default, not a decision: chasing something inside Graph
    means turning `httpx` back on."""
    configure_logging("INFO", library_level="INFO")
    logging.getLogger().handlers[0].setStream(log)  # ty: ignore

    logging.getLogger("httpx").info("HTTP Request: POST https://api 200 OK")

    assert "HTTP Request" in log.getvalue()


def test_the_health_check_does_not_appear_but_the_webhook_does(log: io.StringIO) -> None:
    """The container asks every 30 seconds. An hour of watching an email go
    through is 120 lines saying yes."""
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)

    access.info('127.0.0.1 - "GET %s HTTP/1.1" 200', HEALTH_PATH)
    access.info('40.126.0.1 - "POST /webhooks/outlook HTTP/1.1" 202')

    written = log.getvalue()
    assert HEALTH_PATH not in written
    assert "/webhooks/outlook" in written


def test_configuring_twice_does_not_double_every_line(log: io.StringIO) -> None:
    """`create_app` runs once, but a reload or a test importing it does not."""
    configure_logging("INFO")
    logging.getLogger().handlers[0].setStream(log)  # ty: ignore

    logging.getLogger("src").info("once")

    assert log.getvalue().count("once") == 1


# --- what one email spent -------------------------------------------------


def test_the_tally_is_the_arithmetic_nobody_should_have_to_do() -> None:
    spent = Tally()
    spent.record(ms=1200, input_tokens=4000, output_tokens=120)
    spent.record(ms=800, input_tokens=1500, output_tokens=60)

    assert str(spent) == "2 call(s), 2.0s, 5500->180 tok"


def test_a_provider_that_reports_no_usage_still_counts_the_call() -> None:
    """Token counts are optional; the fact that we paid for a call is not."""
    spent = Tally()
    spent.record(ms=500, input_tokens=None, output_tokens=None)

    assert spent.calls == 1
    assert spent.input_tokens == 0


def test_an_email_that_asked_nothing_says_so() -> None:
    assert str(Tally()) == "no model calls"


async def test_calls_made_inside_a_gather_are_counted_too() -> None:
    """The one that would break silently: a task copies the context, so a
    counter it *replaced* would be lost. Mutating the same object is not."""

    async def ask() -> None:
        spent = tally()
        assert spent is not None
        spent.record(ms=100, input_tokens=10, output_tokens=1)

    with for_one_email("AAMk-1") as spent:
        await asyncio.gather(ask(), ask(), ask())

    assert spent.calls == 3
    assert spent.input_tokens == 30


def test_nothing_is_counted_outside_an_email() -> None:
    """The HTTP endpoint classifies without a handler around it, and the
    startup calls no model at all."""
    assert tally() is None
