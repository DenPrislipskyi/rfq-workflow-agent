"""Push one test case through the real pipeline.

    uv run python test_cases_3/run.py 3        one case
    uv run python test_cases_3/run.py all      all five, in order

The same handler the Graph webhook uses, with the same models, the same
catalogue and the same record store. Two things are replaced, and only two: the
mailbox is a stand-in that serves the folder's files instead of calling Graph,
and forwarding is off - a test case must never put mail in a desk's inbox.

The run writes a record under `Database/`, so the result shows up on the RFQ
screens exactly as a real email would. Read `EXPECTED.md` in the case folder for
what should have happened, and compare.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.core.config import get_settings  # noqa: E402
from src.core.lifespan import (  # noqa: E402
    build_catalog,
    build_extraction,
    build_llm_registry,
    build_matching,
    build_workbooks,
)
from src.core.logging import configure_logging  # noqa: E402
from src.domain.rules.registries import Registries  # noqa: E402
from src.infrastructure.outlook.schemas import EmailMessage  # noqa: E402
from src.infrastructure.storage.decisions import DecisionLog  # noqa: E402
from src.infrastructure.storage.records import EmailRecords  # noqa: E402
from src.services.classification.pipeline import ClassificationPipeline  # noqa: E402
from src.services.handlers import ClassifyingEmailHandler  # noqa: E402
from src.services.triage import EmailTriage  # noqa: E402

HERE = Path(__file__).parent
# Written by `build.py`, read by a person. Neither is an attachment.
NOT_ATTACHMENTS = {"email.txt", "email.json", "EXPECTED.md"}


class StandInMailbox:
    """The folder, pretending to be a mailbox.

    Downloads come off the disk. Forwarding and labelling do nothing and say
    so: a test case that sent real mail or stamped a real message would not be
    a test case for long.
    """

    def __init__(self, address: str, files: dict[str, bytes]) -> None:
        self.address = address
        self._files = files
        self.forwarded: list[str] = []

    async def get_attachment_bytes(self, _message_id: str, attachment_id: str) -> bytes:
        return self._files[attachment_id]

    async def forward(self, _message_id: str, *, to: str, **_kwargs) -> None:
        self.forwarded.append(to)

    async def set_categories(self, _message_id: str, names: list[str]) -> bool:
        return True


async def main(argv: list[str]) -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    wanted = argv[1] if len(argv) > 1 else ""
    cases = _cases(wanted)
    if not cases:
        print(f"No case matches {wanted!r}. Available:\n")
        for folder in _cases("all"):
            print(f"  {folder.name}")
        return 1

    async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
        catalog = build_catalog(settings, client)
        catalog.load()
        llms = build_llm_registry(settings)
        registries = Registries.load(
            settings.REGISTRIES_PATH,
            mailbox=settings.MAILBOX_ADDRESS,
            region_mailboxes=settings.region_mailboxes,
            region_cc=settings.region_cc,
        )
        decisions = DecisionLog(
            settings.DECISIONS_LOG_PATH,
            enabled=settings.PERSIST_DECISIONS,
            log_bodies=settings.LOG_EMAIL_BODIES,
        )
        records = EmailRecords(
            settings.DATABASE_PATH,
            enabled=True,
            keep_attachments=settings.DATABASE_KEEP_ATTACHMENTS,
        )

        for folder in cases:
            await _run_one(folder, settings, llms, registries, decisions, records, catalog)

    return 0


async def _run_one(folder, settings, llms, registries, decisions, records, catalog) -> None:
    print(f"\n{'=' * 78}\n{folder.name}\n{'=' * 78}")

    files = {path.name: path.read_bytes() for path in sorted(folder.iterdir()) if path.name not in NOT_ATTACHMENTS}
    mailbox = StandInMailbox(settings.MAILBOX_ADDRESS, files)

    triage = EmailTriage(
        ClassificationPipeline(llms.text, registries, settings),
        decisions,
        settings.PROMPT_VERSION,
        records=records,
    )
    handler = ClassifyingEmailHandler(
        triage,
        mailbox,
        registries,
        decisions,
        # Never. A case is run to see what the agent decided, not to send mail.
        forward_enabled=False,
        forward_workbook=settings.FORWARD_WORKBOOK,
        reads_attachments=settings.TRIAGE_READS_ATTACHMENTS,
        extraction=build_extraction(settings, llms),
        workbooks=build_workbooks(settings),
        records=records,
        matching=build_matching(settings, llms),
        catalog=lambda: catalog.current,
    )

    await handler.handle(_message(folder, files))
    _report(folder, records)


def _message(folder: Path, files: dict[str, bytes]) -> EmailMessage:
    """The case folder as Graph would have delivered it."""
    header, _, body = (folder / "email.txt").read_text(encoding="utf-8").partition("\n\n")
    fields = dict(
        line.split(": ", 1) for line in header.splitlines() if ": " in line
    )

    return EmailMessage.model_validate(
        {
            "id": f"CASE-{folder.name}",
            "subject": fields.get("Subject", folder.name),
            "from": {"emailAddress": {"address": fields.get("From", "unknown@example.com")}},
            "toRecipients": [{"emailAddress": {"address": fields.get("To", "")}}],
            "body": {"contentType": "text", "content": body},
            "hasAttachments": bool(files),
        }
    ).model_copy(
        update={
            "attachments": [
                _attachment(name, payload) for name, payload in files.items()
            ]
        }
    )


def _attachment(name: str, payload: bytes):
    from src.infrastructure.outlook.schemas import Attachment

    # The id is the filename, which is what the stand-in mailbox looks up.
    return Attachment.model_validate(
        {"id": name, "name": name, "size": len(payload), "contentType": None}
    )


def _report(folder: Path, records: EmailRecords) -> None:
    """What the agent made of it, next to what the case expects."""
    # By message id rather than "the newest": a folder holding a sample or a
    # real email from a minute ago would otherwise be reported as this run.
    wanted = f"CASE-{folder.name}"
    record = next((one for one in records.all() if one.message_id == wanted), None)
    if record is None:
        print("  no record was written")
        return

    verdict = record.verdict
    print(f"\n  verdict     {verdict.category if verdict else '?'}"
          f"   is_rfq={verdict.is_rfq if verdict else '?'}"
          f"   {verdict.recommended_action if verdict else ''}")
    print(f"  labels      {record.labels or 'none'}")
    if record.extraction:
        print(f"  extraction  {record.extraction.items} line(s),"
              f" missing {record.extraction.missing_required or 'nothing'}")
    print(f"  files       {[(f.filename, f.saved_as or f.note) for f in record.attachments]}")
    if record.form:
        print(f"  form        {record.form.saved_as}")

    if not record.matching:
        print("  matching    nothing (not an RFQ, or nothing was read)")
    for line in record.matching:
        print(f"\n  [{line.index}] {line.verbatim[:56]}")
        print(f"       ours  {line.description[:56]}")
        print(f"       code  {line.customer_code or '-':<12} -> {line.item_code or 'NOT MATCHED':<12} ({line.how})")
        print(f"       why   {line.why[:70]}")

    print(f"\n  record      {record.id}")
    print(f"  expected    {folder / 'EXPECTED.md'}")


def _cases(wanted: str) -> list[Path]:
    folders = sorted(p for p in HERE.iterdir() if p.is_dir() and p.name[0].isdigit())
    if wanted in {"all", ""}:
        return folders if wanted == "all" else []
    return [p for p in folders if p.name.startswith(wanted)]


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
