"""Phase 2 in the live path: a mailbox email is read, and the reading is journalled.

The handler is what turns "the code exists" into "sending an email exercises it",
so these tests are about the wiring: the attachments are fetched, the pipeline
runs, the journal gets a line, and none of it can stop the forward.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from src.core.logging import tally
from src.domain.enums import Direction, EmailCategory
from src.domain.rules.registries import Registries
from src.infrastructure.llm.exceptions import LLMCallError
from src.infrastructure.outlook.schemas import Attachment, EmailMessage
from src.infrastructure.storage.decisions import DecisionLog
from src.services.classification.pipeline import ClassificationPipeline
from src.services.classification.schemas import LLMClassification
from src.services.extraction import ExtractionPipeline, FileReader, HeaderReader
from src.services.extraction.models import HeaderField, ItemField
from src.services.extraction.schemas import (
    ColumnAssignment,
    TableMapping,
    ExtractedField,
    FileRead,
    HeaderExtraction,
    MissingField)
from src.infrastructure.excel import Template
from src.infrastructure.storage.workbooks import WorkbookStore
from src.services.handlers import ClassifyingEmailHandler
from src.services.triage import EmailTriage
from src.services.workbook import WorkbookBuilder
from tests.workbook_builder import master
from tests import attachments_builder as build
from tests.fakes import FakeLLM, fake_settings

REGISTRIES = Registries.load(
    Path("config/registries.yaml"),
    mailbox="supply@our-company.com",
    region_mailboxes={"uae": "dst.uae@our-company.com"})


RFQ_VERDICT = LLMClassification(
    category=EmailCategory.NEW_RFQ,
    direction=Direction.INBOUND_CUSTOMER,
    is_rfq=True,
    confidence=0.95,
    reasoning="Customer asks the chandler to quote the attached requisition.")

MAPPING = TableMapping(
    header_row=3,
    assignments=[
        ColumnAssignment(column="B", field=ItemField.CUSTOMER_ITEM_CODE),
        ColumnAssignment(column="C", field=ItemField.DESCRIPTION),
        ColumnAssignment(column="D", field=ItemField.QUANTITY),
        ColumnAssignment(column="E", field=ItemField.UOM),
    ])

# Long enough that the empty-body rule does not answer it: this test is about
# what an email with no files costs the model, not about the fast path.
SPAM_BODY = "Our new range of marine paints is now available. Ask for our catalogue."

HEADER = HeaderExtraction(
    fields=[
        ExtractedField(field=HeaderField.VESSEL_NAME, value="MV ALMI GLOBE", source="email.subject"),
        ExtractedField(field=HeaderField.DELIVERY_PORT, value="Jebel Ali", source="email.body"),
    ]
)


class Router:
    """One double answering every question by the shape it was asked for.

    `FileRead` answers by what it was shown, because the pipeline reviews the
    email body alongside the attachments and only one of them is a requisition.
    Every prompt is kept, so a test can ask what the classifier was told about
    the files rather than only what it answered.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.seen: dict[str, object] = {}

    async def invoke(self, messages, schema):
        self.asked.append(schema.__name__)
        self.seen[schema.__name__] = messages

        if schema.__name__ == "FileRead":
            shown = messages[1][1]
            shown = shown if isinstance(shown, str) else shown[0]["text"]
            answer = (
                FileRead(what="A requisition", has_item_list=True, tables=[MAPPING])
                if "Requisition.xlsx" in shown
                else FileRead(what="The covering message", has_item_list=False)
            )
        else:
            answer = {
                "LLMClassification": RFQ_VERDICT,
                "HeaderExtraction": HEADER,
            }[schema.__name__]

        return await FakeLLM(answer).invoke(messages, schema)


class Verdict(Router):
    """The same double, answering the triage question with a chosen category.

    Only the verdict differs: what the files hold is still read out of them, so
    a test can put a brochure and a requisition through the identical wiring.
    """

    def __init__(self, category: EmailCategory, what: str = "The covering message") -> None:
        super().__init__()
        self.category = category
        self.what = what

    async def invoke(self, messages, schema):
        if schema.__name__ == "LLMClassification":
            self.asked.append(schema.__name__)
            self.seen[schema.__name__] = messages
            return await FakeLLM(
                RFQ_VERDICT.model_copy(
                    update={
                        "category": self.category,
                        "is_rfq": False,
                        "reasoning": "Not a customer request for a quotation.",
                    }
                )
            ).invoke(messages, schema)
        if schema.__name__ == "FileRead":
            self.asked.append(schema.__name__)
            self.seen[schema.__name__] = messages
            return await FakeLLM(
                FileRead(what=self.what, has_item_list=False)
            ).invoke(messages, schema)
        return await super().invoke(messages, schema)


class Unreadable(Router):
    """A document model that will not answer about a file, however often asked.

    A corrupt PDF, a provider outage mid-email, a schema the model cannot
    satisfy - all of them end here, and all of them must leave the file
    reported rather than silently missing.
    """

    async def invoke(self, messages, schema):
        if schema.__name__ == "FileRead":
            self.asked.append(schema.__name__)
            raise LLMCallError("document model", RuntimeError("boom"))
        return await super().invoke(messages, schema)


class Sent(NamedTuple):
    to: str
    comment: str
    attachment: object | None


class FakeMailbox:
    """A mailbox that hands back one attachment's bytes."""

    address = "supply@our-company.com"

    def __init__(self, *, download_fails: bool = False, forward_fails: bool = False) -> None:
        self.labels: list[list[str]] = []
        self.forwards: list[Sent] = []
        self.downloaded: list[str] = []
        self._download_fails = download_fails
        self._forward_fails = forward_fails

    async def set_categories(self, message_id: str, categories: list[str]) -> None:
        self.labels.append(categories)

    async def forward(
        self,
        message_id: str,
        *,
        to: str,
        cc: Sequence[str] = (),
        comment: str = "",
        attachment=None) -> None:
        if self._forward_fails:
            raise RuntimeError("Graph said no")
        self.forwards.append(Sent(to, comment, attachment))

    async def get_attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        if self._download_fails:
            raise RuntimeError("Graph said no")
        self.downloaded.append(attachment_id)
        return build.xlsx_requisition()


class RefusesAttachments(FakeMailbox):
    """A mailbox that takes a plain forward and rejects one carrying a file.

    Exchange refusing a 12.5 MB message, or Defender stripping a macro-enabled
    workbook - the two ways this is expected to fail in the field.
    """

    async def forward(self, message_id: str, *, to: str, cc=(), comment: str = "",
                      attachment=None) -> None:
        if attachment is not None:
            raise RuntimeError("message size exceeds the limit")
        await super().forward(message_id, to=to, cc=cc, comment=comment)


def message(*attachments: Attachment, **overrides) -> EmailMessage:
    """One inbound email. Overrides are Graph's own field names."""
    return EmailMessage.model_validate(
        {
            "id": "AAMk-1",
            "subject": "RFQ 78432 / MV ALMI GLOBE / Jebel Ali",
            "from": {"emailAddress": {"address": "purchasing@almiship.com"}},
            "toRecipients": [{"emailAddress": {"address": "supply@our-company.com"}}],
            "body": {"contentType": "text", "content": "Please quote the attached."},
            "hasAttachments": bool(attachments),
        }
        | overrides
    ).model_copy(update={"attachments": list(attachments)})


def attachment(name: str = "Requisition.xlsx", **kwargs) -> Attachment:
    return Attachment.model_validate(
        {"id": "att-1", "name": name, "size": 5000, "contentType": None} | kwargs
    )


def build_handler(
    tmp_path: Path,
    *,
    extraction: bool = True,
    mailbox=None,
    workbooks: bool = True,
    llm=None,
    reads_attachments: bool = True,
):
    settings = fake_settings()
    llm = llm or Router()
    journal = DecisionLog(tmp_path / "decisions.jsonl", enabled=True, log_bodies=False)
    triage = EmailTriage(
        ClassificationPipeline(llm, REGISTRIES, settings), journal, settings.PROMPT_VERSION
    )
    box = mailbox or FakeMailbox()

    pipeline = None
    if extraction:
        pipeline = ExtractionPipeline(
            files=FileReader(llm), header=HeaderReader(llm)
        )

    builder = None
    if workbooks:
        builder = WorkbookBuilder(
            Template(master()), WorkbookStore(tmp_path / "workbooks", enabled=True)
        )

    handler = ClassifyingEmailHandler(
        triage,
        box,
        REGISTRIES,
        journal,
        forward_enabled=True,
        reads_attachments=reads_attachments,
        extraction=pipeline,
        workbooks=builder)
    return handler, box, journal


def triage_prompt(handler) -> str:
    """The user turn the classifier was given, out of the double that answered it."""
    llm = handler._triage._pipeline._llm  # noqa: SLF001
    return llm.seen["LLMClassification"][-1][1]


def extraction_line(journal: DecisionLog) -> dict | None:
    return next(iter(journal.extractions()), None)


def file_line(journal: DecisionLog, name: str) -> dict:
    """One document from the journal, by name rather than by position.

    The email body is only read when no attachment carried the item list, so
    what is in the list and in what order depends on the email.
    """
    return next(
        item for item in extraction_line(journal)["documents"] if name in item["origin"]
    )


# --- the wiring -----------------------------------------------------------


async def test_an_rfq_with_an_attachment_is_read_and_journalled(tmp_path: Path):
    handler, box, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    assert box.downloaded == ["att-1"], "the attachment's bytes were fetched"
    line = extraction_line(journal)
    assert line is not None
    assert line["items"]["count"] == 3


async def test_the_extraction_line_ties_to_the_decision_that_produced_it(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    decision = next(journal.decisions())
    assert extraction_line(journal)["decision_id"] == decision["decision_id"]


async def test_the_rfq_is_still_forwarded(tmp_path: Path):
    """Reading must not become a way for the desk to stop receiving RFQs."""
    handler, box, _ = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    assert box.forwards


async def test_extraction_switched_off_leaves_phase_one_untouched(tmp_path: Path):
    handler, box, journal = build_handler(tmp_path, extraction=False)

    await handler.handle(message(attachment()))

    assert box.forwards
    assert box.downloaded == [], "nothing is downloaded when nothing will read it"
    assert extraction_line(journal) is None


async def test_a_non_rfq_with_nothing_attached_costs_one_call_and_no_download(
    tmp_path: Path):
    """The cheapest email there is: one verdict, no files, nothing fetched."""
    handler, box, journal = build_handler(tmp_path, llm=Verdict(EmailCategory.SPAM_MARKETING))

    await handler.handle(message(body={"contentType": "text", "content": SPAM_BODY}))

    assert handler._triage._pipeline._llm.asked == ["LLMClassification"]  # noqa: SLF001
    assert box.downloaded == []
    assert extraction_line(journal) is None


async def test_a_brochure_is_read_and_the_email_still_is_not_an_rfq(tmp_path: Path):
    """What reading before the verdict costs: a marketing email with a file
    attached now pays one call for the file. It buys the case below, and the
    fast path in front of it is what bounds the bill."""
    llm = Verdict(EmailCategory.SPAM_MARKETING, what="A product brochure for marine paints")
    handler, box, journal = build_handler(tmp_path, llm=llm)

    await handler.handle(message(attachment("Brochure.pdf")))

    assert llm.asked == ["FileRead", "LLMClassification"], "the file, then the verdict"
    assert box.downloaded == ["att-1"]
    assert "holds a list of items: no" in triage_prompt(handler)
    assert extraction_line(journal) is None, "nothing is extracted from a non-RFQ"
    assert box.forwards == []


async def test_a_requisition_in_a_file_reaches_the_verdict_with_its_row_count(
    tmp_path: Path):
    """The email a body-only classifier gets wrong: the body is empty and the
    whole demand is in the attachment."""
    handler, _, _ = build_handler(tmp_path)

    await handler.handle(message(attachment(), body={"contentType": "text", "content": ""}))

    prompt = triage_prompt(handler)
    assert "Requisition.xlsx - A requisition" in prompt, "the file and what it is"
    assert "holds a list of items: yes, 3 row(s)" in prompt


async def test_a_file_nobody_could_read_is_shown_as_unknown_rather_than_absent(
    tmp_path: Path):
    """A parser that fails must not look like an email with nothing in it. The
    prompt turns "unknown" into a person's problem, never into "not an RFQ"."""
    handler, box, _ = build_handler(tmp_path, llm=Unreadable())

    await handler.handle(message(attachment()))

    prompt = triage_prompt(handler)
    assert "could not be read" in prompt
    assert "holds a list of items: unknown" in prompt
    assert box.downloaded == ["att-1"], "it was fetched and parsed, only not read"


async def test_the_files_are_read_once_and_the_extraction_reuses_that_reading(
    tmp_path: Path):
    """The reason reading moved in front of triage rather than being done twice:
    one call per file, whichever step needed it."""
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    asked = handler._triage._pipeline._llm.asked  # noqa: SLF001
    assert asked == ["FileRead", "LLMClassification", "HeaderExtraction"]
    assert extraction_line(journal)["items"]["count"] == 3


async def test_an_email_the_rules_answer_is_never_downloaded(tmp_path: Path):
    """The cost guard, unchanged: internal chatter with a spreadsheet on it
    costs nothing at all, because no model is asked and so no file matters."""
    handler, box, journal = build_handler(tmp_path)
    internal = message(
        attachment(),
        **{
            "from": {"emailAddress": {"address": "robert.hall@our-company.com"}},
            "toRecipients": [{"emailAddress": {"address": "michael.reed@our-company.com"}}],
        })

    await handler.handle(internal)

    assert handler._triage._pipeline._llm.asked == []  # noqa: SLF001
    assert box.downloaded == []
    assert next(journal.decisions())["attachments_read"] is None


async def test_the_switch_puts_the_verdict_back_on_the_message_alone(tmp_path: Path):
    """`TRIAGE_READS_ATTACHMENTS=false`. The files are still read, only after
    the verdict rather than before it - which is Phase 1's order."""
    handler, box, journal = build_handler(tmp_path, reads_attachments=False)

    await handler.handle(message(attachment()))

    asked = handler._triage._pipeline._llm.asked  # noqa: SLF001
    assert asked == ["LLMClassification", "FileRead", "HeaderExtraction"]
    assert "<attachments" not in triage_prompt(handler)
    assert next(journal.decisions())["attachments_read"] is None
    assert extraction_line(journal)["items"]["count"] == 3, "still read, still filled"


async def test_the_decision_line_says_what_the_verdict_knew_about_the_files(
    tmp_path: Path):
    """Six months on, "why did this look like an RFQ?" has to be answerable
    from the journal, and for these emails the answer is in the attachment."""
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    seen = next(journal.decisions())["attachments_read"]
    assert seen == [
        {
            "origin": "Requisition.xlsx",
            "role": "ITEM_GRID",
            "what": "A requisition",
            "items": 3,
        }
    ]


async def test_a_download_that_fails_before_the_verdict_still_gets_a_verdict(
    tmp_path: Path):
    """Graph refusing the bytes is a reason to classify on the text, not a
    reason to drop the email."""
    handler, box, journal = build_handler(
        tmp_path, mailbox=FakeMailbox(download_fails=True)
    )

    await handler.handle(message(attachment()))

    assert next(journal.decisions())["result"]["category"] == "NEW_RFQ"
    assert box.forwards, "the desk still gets it"


# --- nothing here may cost the forward ------------------------------------


async def test_an_attachment_that_will_not_download_is_a_warning_not_a_crash(tmp_path: Path):
    handler, box, journal = build_handler(
        tmp_path, mailbox=FakeMailbox(download_fails=True)
    )

    await handler.handle(message(attachment()))

    assert box.forwards, "the desk still gets the RFQ"
    assert "attachment_has_no_bytes" in file_line(journal, "Requisition.xlsx")["warnings"]


async def test_an_attached_email_is_fetched_and_unpacked(tmp_path: Path):
    """Measured on a live run: a forwarded "FW: RFQ" arrived as an item
    attachment, `_download` skipped it because it is not a file attachment, and
    the requisition inside was never seen at all. `/$value` returns the message
    as MIME, and stage A already knows what to do with that."""

    class Forwarded(FakeMailbox):
        async def get_attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
            self.downloaded.append(attachment_id)
            return build.eml_with_attachment(build.xlsx_requisition(), "Requisition.xlsx")

    handler, box, journal = build_handler(tmp_path, mailbox=Forwarded())
    attached_email = attachment(
        "FW RFQ 78432", **{"@odata.type": "#microsoft.graph.itemAttachment"}
    )

    await handler.handle(message(attached_email))

    assert box.downloaded == ["att-1"], "an attached email has bytes after all"
    origins = [item["origin"] for item in extraction_line(journal)["documents"]]
    assert "FW RFQ 78432 > Requisition.xlsx" in origins
    assert extraction_line(journal)["items"]["count"] == 3


async def test_a_reference_attachment_is_reported_without_being_fetched(tmp_path: Path):
    handler, box, journal = build_handler(tmp_path)
    link = attachment(**{"@odata.type": "#microsoft.graph.referenceAttachment"})

    await handler.handle(message(link))

    assert box.downloaded == []
    assert "attachment_is_a_link" in file_line(journal, "Requisition.xlsx")["warnings"]


# --- what the journal line has to answer ----------------------------------


async def test_the_line_says_what_each_file_was_and_what_came_out_of_it(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    requisition = file_line(journal, "Requisition.xlsx")

    assert requisition["kind"] == "XLSX"
    assert requisition["read"]["grids"] == 1
    assert requisition["role"] == "ITEM_GRID"
    assert requisition["what"] == "A requisition"
    assert requisition["items"] == 3


async def test_the_email_body_is_only_read_when_no_attachment_carried_the_list(
    tmp_path: Path):
    """One call saved on every ordinary RFQ, and the gap still covered."""
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    origins = [item["origin"] for item in extraction_line(journal)["documents"]]
    assert origins == ["Requisition.xlsx"]


async def test_the_line_shows_a_header_field_as_written_and_as_written_in(tmp_path: Path):
    """The pair that answers "why does the cell say that?" - and with nothing
    looked up anywhere, the two are the same."""
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    port = extraction_line(journal)["header"]["delivery_port"]
    assert port["raw"] == "Jebel Ali"
    assert port["source"] == "email.body"
    assert port["value"] == "Jebel Ali"


async def test_the_line_names_the_starred_cells_that_will_go_out_blank(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    line = extraction_line(journal)
    assert line["complete"] is False
    assert "imo" in line["missing_required"]


async def test_the_line_says_what_the_models_cost(tmp_path: Path):
    """The plan claims an ordinary RFQ costs three calls - the file, the
    verdict, the header. This is where that claim is checked against a live
    run, so the count has to be on the line."""

    class Billing(Router):
        """A double that bills like the real client does."""

        async def invoke(self, messages, schema):
            if (spent := tally()) is not None:
                spent.record(ms=1000, input_tokens=100, output_tokens=10)
            return await super().invoke(messages, schema)

    handler, _, journal = build_handler(tmp_path, llm=Billing())

    await handler.handle(message(attachment()))

    assert extraction_line(journal)["spend"] == {
        "calls": 3,
        "input_tokens": 300,
        "output_tokens": 30,
        "ms": 3000,
    }


async def test_the_line_says_where_the_items_came_from(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    sources = extraction_line(journal)["items"]["sources"]
    assert sources == [
        "Requisition.xlsx#Requisition row 4",
        "Requisition.xlsx#Requisition row 5",
        "Requisition.xlsx#Requisition row 6",
    ]


# --- what the operator's file supplies ------------------------------------


async def test_a_currency_nobody_named_is_left_empty(tmp_path: Path):
    """No branch default: the desk asked for none, and inventing a currency is
    how an RFQ comes back priced in the wrong money."""
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    assert "currency" not in extraction_line(journal)["header"]
    assert "currency" in extraction_line(journal)["missing_required"]


# --- the file that comes out of it ----------------------------------------


async def test_an_rfq_leaves_a_filled_workbook_behind(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    workbook = extraction_line(journal)["workbook"]
    assert workbook["items"] == 3
    assert workbook["filename"].startswith("POC-"), "the test RFQ quotes no reference"
    assert Path(workbook["saved_to"]).exists()


async def test_the_copy_on_disk_is_named_after_the_decision_that_made_it(tmp_path: Path):
    """The journal line and the file it describes have to be findable from
    each other, and only the decision id is in both."""
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    line = extraction_line(journal)
    assert Path(line["workbook"]["saved_to"]).stem == line["decision_id"]


async def test_the_line_says_which_master_the_copy_was_cut_from(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    assert len(extraction_line(journal)["workbook"]["template_sha256"]) == 64


async def test_a_template_that_could_not_be_loaded_costs_the_file_and_nothing_else(
    tmp_path: Path):
    handler, box, journal = build_handler(tmp_path, workbooks=False)

    await handler.handle(message(attachment()))

    assert box.forwards
    assert "workbook" not in extraction_line(journal)
    assert extraction_line(journal)["items"]["count"] == 3


async def test_a_workbook_that_will_not_write_still_leaves_the_reading_journalled(
    tmp_path: Path):
    """A file we could not produce is a thing to explain, not a thing to hide."""
    handler, box, journal = build_handler(tmp_path)

    async def refuse(*args, **kwargs):
        raise RuntimeError("out of disk")

    handler._workbooks.build = refuse  # noqa: SLF001

    await handler.handle(message(attachment()))

    assert box.forwards
    assert "workbook" not in extraction_line(journal)
    assert extraction_line(journal)["items"]["count"] == 3


# --- the forward that carries it ------------------------------------------


async def test_the_form_goes_out_with_the_forward(tmp_path: Path):
    handler, box, _ = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    sent = box.forwards[0]
    assert sent.attachment is not None
    assert sent.attachment.filename.startswith("POC-")
    assert sent.attachment.content_type.endswith("spreadsheetml.sheet")


async def test_the_desk_is_told_what_the_form_is_and_what_it_lacks(tmp_path: Path):
    handler, box, _ = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    note = box.forwards[0].comment
    assert ".xlsx" in note
    assert "3 item(s)" in note
    assert "IMO Number" in note, "the starred cells left blank are named, not numbered"


async def test_the_forwarded_email_says_why_each_cell_is_empty(tmp_path: Path):
    """"IMO Number" sends somebody hunting through the email again. "IMO
    Number: not stated anywhere, the vessel is named but never numbered" does
    not - and only the reader that saw the email can say that."""

    class Explains(Router):
        async def invoke(self, messages, schema):
            if schema.__name__ == "HeaderExtraction":
                self.asked.append(schema.__name__)
                return await FakeLLM(
                    HeaderExtraction(
                        fields=HEADER.fields,
                        not_found=[
                            MissingField(
                                field=HeaderField.IMO,
                                why="Not stated anywhere - the vessel is named but never numbered.",
                            )
                        ],
                    )
                ).invoke(messages, schema)
            return await super().invoke(messages, schema)

    handler, box, journal = build_handler(tmp_path, llm=Explains())

    await handler.handle(message(attachment()))

    note = box.forwards[0].comment
    assert "IMO Number: Not stated anywhere" in note
    # And the same sentence is on the journal line, so a run can be reviewed
    # without opening every workbook.
    assert any(
        "Not stated anywhere" in line
        for line in extraction_line(journal)["workbook"]["why_blank"]
    )


async def test_explaining_the_blanks_costs_no_extra_model_call(tmp_path: Path):
    """It rides in the header answer. A separate call would be a second thing
    to fail, and the header reader is the only one that read everything."""
    handler, _, _ = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    asked = handler._triage._pipeline._llm.asked  # noqa: SLF001
    assert asked == ["FileRead", "LLMClassification", "HeaderExtraction"]


async def test_the_switch_sends_the_rfq_without_the_form(tmp_path: Path):
    """A separate switch from FORWARD_ENABLED: 12.5 MB against Exchange's 35 MB
    default is its own risk, and turning it off must not stop the forwards."""
    handler, box, journal = build_handler(tmp_path)
    handler._forward_workbook = False  # noqa: SLF001

    await handler.handle(message(attachment()))

    assert box.forwards[0].attachment is None
    assert box.forwards[0].comment == ""
    assert extraction_line(journal)["workbook"]["items"] == 3, "still filled and kept"


async def test_a_form_that_will_not_send_costs_the_form_and_not_the_rfq(tmp_path: Path):
    """The desk can quote from the customer's own attachments. It cannot quote
    from an email that never arrived."""
    handler, box, journal = build_handler(tmp_path, mailbox=RefusesAttachments())

    await handler.handle(message(attachment()))

    assert len(box.forwards) == 1
    assert box.forwards[0].attachment is None
    assert "could not be attached" in box.forwards[0].comment
    assert next(journal.deliveries())["outcome"] == "SENT"
    assert next(journal.deliveries())["attached"] is None


async def test_a_label_that_would_not_stick_is_reported_as_not_stuck(tmp_path: Path):
    """Measured in a live run: Exchange refused the label with 412 and the
    journal still said `labels: ["SSG RFQ"]`. That label is what a person sees
    in Outlook **and** the dedupe that stops a second forward - claiming it
    when it is not there is the one thing this line must not do."""

    class RefusesLabels(FakeMailbox):
        async def set_categories(self, message_id: str, categories: list[str]) -> None:
            raise RuntimeError("Graph API error 412: ErrorIrresolvableConflict")

    handler, box, journal = build_handler(tmp_path, mailbox=RefusesLabels())

    await handler.handle(message(attachment()))

    delivery = next(journal.deliveries())
    assert delivery["labelled"] is False
    assert delivery["labels"] == ["SSG RFQ"], "what we meant to write is still on the line"
    assert delivery["outcome"] == "SENT", "the RFQ did reach the desk"


async def test_a_label_that_stuck_says_so(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    assert next(journal.deliveries())["labelled"] is True


async def test_a_sent_form_is_named_on_the_delivery_line(tmp_path: Path):
    handler, _, journal = build_handler(tmp_path)

    await handler.handle(message(attachment()))

    assert next(journal.deliveries())["attached"].endswith(".xlsx")
