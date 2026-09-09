"""How the service talks about itself while it runs.

Three separate jobs, and the reason they live together is that all three exist
to answer one question after a live run: **what happened to this email, and
why?**

1. **The format** carries a column naming the email every line belongs to.
   Without it, two emails arriving a second apart interleave into one
   unreadable stream - the pipeline awaits network on every step, so their
   lines are guaranteed to mix.
2. **The libraries are muted.** `httpx` logs a line per request, and there are
   eight Graph calls and up to eight model calls per RFQ; `pdfminer` logs per
   glyph run. At INFO they bury our own lines, and at DEBUG they make the log
   unusable - which is exactly when somebody has turned DEBUG on to read it.
3. **The tally** counts what one email spent on models, so the closing line can
   say "5 call(s), 9.4s" instead of leaving the arithmetic to a reader.

Nothing here is required for the service to work. It is required for the run to
be readable afterwards, which is the only way anyone learns anything from it.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# The email column sits before the module name: a reader scans down it to
# follow one email, and the width keeps the columns aligned while doing so.
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(email)-19s | %(name)s | %(message)s"

# No email is being handled - startup, shutdown, the HTTP endpoint.
NO_EMAIL = "-"

# One line per HTTP request, per PDF page, per image. Every one of them is a
# line about how the work was done rather than what was decided, and the
# provider's own logs are a better place to look when it matters.
NOISY_LIBRARIES = (
    "asyncio",
    "httpx",
    "httpcore",
    "openai",
    "anthropic",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_anthropic",
    "langsmith",
    "msal",
    "urllib3",
    "pdfminer",
    "pdfplumber",
    "PIL",
    "openpyxl",
    "python_multipart",
)

# The container asks every 30 seconds whether we are alive. Over an hour of
# watching three emails go through, that is 120 lines saying yes.
HEALTH_PATH = "/health-check"

_TAG: ContextVar[str] = ContextVar("email_tag", default=NO_EMAIL)


@dataclass(slots=True)
class Tally:
    """What one email spent on models, added up as the calls come back.

    Mutable and held in a context variable rather than passed down, because the
    calls happen four layers below the handler and two of them run inside
    `asyncio.gather` - a task copies the context, so a value set in a task
    would not come back, while a mutable object in it accumulates from either
    side.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0

    def record(
        self, *, ms: int, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        """One answered call. Providers that report no usage still count."""
        self.calls += 1
        self.ms += ms
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0

    def __str__(self) -> str:
        if not self.calls:
            return "no model calls"
        return (
            f"{self.calls} call(s), {self.ms / 1000:.1f}s, "
            f"{self.input_tokens}->{self.output_tokens} tok"
        )


_TALLY: ContextVar[Tally | None] = ContextVar("email_tally", default=None)


def configure_logging(level: str = "INFO", library_level: str = "WARNING") -> None:
    """Install the one handler everything logs through.

    `force=True` because uvicorn is already running by the time the app is
    imported: without it `basicConfig` finds a root handler, does nothing at
    all, and every line comes out in somebody else's format with no email
    column in it.
    """
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, force=True)

    for handler in logging.getLogger().handlers:
        handler.addFilter(_stamp_the_email)

    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(library_level.upper())

    # Kept at INFO, unlike the rest: one line per webhook POST is how you see
    # that Graph is still talking to us at all. Only the health check goes.
    logging.getLogger("uvicorn.access").addFilter(_drop_health_checks)


@contextmanager
def for_one_email(message_id: str | None) -> Iterator[Tally]:
    """Tag every line logged inside with this email, and tally what it spends.

    The tag is the tail of the Graph message id - the head is the same for
    every message in a mailbox, and the whole thing is 150 characters wide.
    """
    tally = Tally()
    tag = _TAG.set(_short(message_id))
    counter = _TALLY.set(tally)
    try:
        yield tally
    finally:
        _TAG.reset(tag)
        _TALLY.reset(counter)


def also_the_decision(decision_id: str | None) -> None:
    """Put the journal's id in the tag as well, once triage has made one.

    Both ids, because they answer different questions: the message id is what
    Outlook and Graph know this email as, and the decision id is the name of
    the journal line and of the workbook on disk.
    """
    if decision_id:
        _TAG.set(f"{_TAG.get()}/{decision_id[:8]}")


def tally() -> Tally | None:
    """The tally of the email being handled, or None outside one."""
    return _TALLY.get()


def _short(message_id: str | None) -> str:
    return message_id[-10:] if message_id else NO_EMAIL


def _stamp_the_email(record: logging.LogRecord) -> bool:
    """Give every record the column the format asks for.

    On the handler rather than on our own loggers, so that a library line
    formatted by this handler has the attribute too - a missing one is a
    formatting error at write time, which is a broken log rather than a
    missing field.
    """
    record.email = _TAG.get()
    return True


def _drop_health_checks(record: logging.LogRecord) -> bool:
    return HEALTH_PATH not in record.getMessage()
