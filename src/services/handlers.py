"""What happens to an email once it has been fetched from the mailbox."""

import logging
from dataclasses import dataclass, field
from html import escape

from src.core.logging import also_the_decision, for_one_email, tally
from src.domain.enums import DeliveryOutcome, RecommendedAction
from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.domain.rules.regions import RegionMatch, resolve_region
from src.domain.rules.registries import Registries
from src.infrastructure.documents import Budget, Document, DocumentLoader, SourceFile
from src.infrastructure.outlook.mailbox import Mailbox, OutgoingFile
from src.infrastructure.outlook.mapping import to_normalized_email
from src.infrastructure.outlook.schemas import Attachment, EmailMessage
from src.infrastructure.storage.decisions import DecisionLog
from src.services.extraction import ExtractionPipeline, ReadDocument, RfqExtraction
from src.services.triage import EmailTriage
from src.services.workbook import CONTENT_TYPE, FilledWorkbook, WorkbookBuilder

logger = logging.getLogger(__name__)

# The three outcomes that mean "we tried and could not". A switch left off, or a
# verdict a person still has to confirm, is not a failed delivery.
UNDELIVERED = frozenset(
    {DeliveryOutcome.NO_REGION, DeliveryOutcome.NO_ADDRESS, DeliveryOutcome.FAILED}
)

# What the desk reads above the forwarded email. Three lines at most: what the
# file is, what it does not have in it, and that a machine filled it. Anything
# longer stops being read, and the customer's own email is the point of the
# message.
ATTACHED = "Auto-filled KASS RFQ form attached: {filename} ({items} item(s))."
NOT_ATTACHED = (
    "An auto-filled KASS RFQ form was produced but could not be attached to this "
    "forward. It is on the agent's disk under decision {decision_id}."
)
STILL_BLANK = "Left blank, please complete by hand:"
# Six starred cells plus whatever was refused. Past this the note stops being
# read, and the block under the form carries the rest anyway.
MAX_BLANKS_LISTED = 6
NOTHING_READ = "No line items could be read out of this RFQ - the item grid is empty."
# The desk reads this before it opens the form. A file that arrived and could
# not be read may be the requisition itself, so it is named here rather than
# only in the block under the form.
NOT_READ = "{count} attached file(s) could not be read - please check by hand: {files}."
# The most expensive thing on this list: two attachments giving one article two
# quantities. Both rows are in the grid, because guessing which revision stands
# is not code's call - but nobody may quote from it without being told.
DISAGREE = "Two attachments disagree on a quantity - check before quoting: {items}."
CAVEAT = "Filled by an automated agent - check it against the customer's own files."

# An email may carry twenty files, and the arrival line is not the inventory:
# the reading that follows names every one of them anyway.
MAX_NAMES_LOGGED = 5


@dataclass(frozen=True, slots=True)
class Delivery:
    """Where an RFQ went, and by which rule it was routed there."""

    outcome: DeliveryOutcome
    forwarded_to: str | None = None
    cc: list[str] = field(default_factory=list)
    region: str | None = None
    region_rule: str | None = None
    # The filled form, if it went with the email. None covers three different
    # things - none was made, the switch is off, attaching it failed - and the
    # log line beside this one says which.
    attached: str | None = None


class ClassifyingEmailHandler:
    """Runs a mailbox email through the same triage the HTTP endpoint uses, then
    acts on the verdict: forwards the RFQs and labels every message."""

    def __init__(
        self,
        triage: EmailTriage,
        mailbox: Mailbox,
        registries: Registries,
        decisions: DecisionLog,
        *,
        forward_enabled: bool = False,
        forward_workbook: bool = True,
        reads_attachments: bool = True,
        extraction: ExtractionPipeline | None = None,
        workbooks: WorkbookBuilder | None = None,
        budget: Budget | None = None,
    ) -> None:
        self._triage = triage
        self._mailbox = mailbox
        self._categories = registries.outlook_categories
        self._regions = registries.regions
        self._decisions = decisions
        self._forward_enabled = forward_enabled
        self._forward_workbook = forward_workbook
        # Whether the files are read before the verdict rather than after it.
        # Off, the classifier sees the message alone, exactly as in Phase 1.
        self._reads_attachments = reads_attachments
        # None means Phase 2 is switched off: the email is still classified,
        # labelled and forwarded, just not read. The reader lives here, so this
        # also decides whether there is anything to show the verdict.
        self._extraction = extraction
        # None again when the master template could not be loaded. The RFQ is
        # still read and journalled; there is simply no file at the end of it.
        self._workbooks = workbooks
        self._loader = DocumentLoader(budget)

    async def handle(self, message: EmailMessage) -> None:
        """Read the files, classify, deliver if it is an RFQ, label, and journal.

        The order is the whole design:

            hard rules  ->  download  ->  read each file  ->  is this an RFQ?

        The rules stay in front because they are the only step that costs
        nothing, and an auto-reply must not download a thing. Everything they
        leave undecided has its files read first, so that the verdict is taken
        with the requisition in view rather than from a body that says "please
        find attached" - and the same reading is handed to the extraction
        afterwards, so no file is read twice.

        Forwarding runs before labelling on purpose: crashing in between then
        risks a duplicate rather than a silent loss, and by this project's own
        arithmetic a duplicate costs an operator seconds while a lost RFQ costs
        a sale. A failed classification is labelled too, and re-raised for
        `NotificationService` to log - one bad email must not stop the queue.
        """
        if self._already_handled(message):
            logger.info("Message %s is already labelled, skipping", message.id)
            return

        # Every line logged from here down carries this email's tag, however
        # deep it was written: the pipeline awaits network on every step, so
        # two emails arriving together are guaranteed to interleave.
        with for_one_email(message.id) as spent:
            email = to_normalized_email(message, mailbox=self._mailbox.address)
            # Logged before any work starts, because the work takes tens of
            # seconds and a reader watching a live run needs to know the email
            # arrived. The subject is on the verdict line, not repeated here.
            logger.info(
                "Handling | from %s | %s",
                email.sender.address if email.sender else "unknown sender",
                _attached(message),
            )
            files = await self._read(message, email)

            try:
                triaged = await self._triage.run(email, source="outlook", files=files)
            except Exception:
                # No decision id yet - the classification is what failed.
                await self._label(message.id, self._categories.for_failure())
                raise

            also_the_decision(triaged.decision_id)
            outcome = triaged.outcome
            labels = self._categories.for_result(outcome.result)
            delivery = None
            workbook = None

            if outcome.result.recommended_action is RecommendedAction.FORWARD_TO_DST:
                match = self._region(email, outcome)
                workbook = await self._extract(
                    message, email, outcome, match, triaged.decision_id, files
                )
                delivery = await self._deliver(
                    message.id, match, triaged.decision_id, outcome, workbook
                )
                if delivery.outcome in UNDELIVERED:
                    labels = self._categories.plus_not_sent(labels)

            labelled = await self._label(message.id, labels)

            if delivery is not None:
                await self._journal(triaged.decision_id, delivery, labels, labelled)

            # The line a whole run is read by: `grep Done` gives one row per
            # email, saying what it was taken for, what came out of it, where
            # it went and what it cost. Everything in it appears in the lines
            # above as well, on purpose - those are the working, this is the
            # answer.
            logger.info(
                "Done | %s | %s | %s | %s %s | %s | %s",
                outcome.result.category.value,
                outcome.result.recommended_action.value,
                _filled(workbook),
                "labelled" if labelled else "LABEL FAILED",
                labels or "nothing",
                delivery.outcome.value if delivery else "not an RFQ",
                spent,
            )

    async def _read(
        self, message: EmailMessage, email: NormalizedEmail
    ) -> list[ReadDocument] | None:
        """Every attachment, read, before the verdict - or None for "not read".

        None on four counts: the switch is off, Phase 2 is off and there is no
        reader, the email has no files, or the hard rules have already answered
        it and nothing may be spent on it. Then the classifier works from the
        message alone, and the journal line says `null` rather than `[]` - the
        log above says which of the four it was.

        Never raises. Graph refusing a download is a reason to classify on the
        text, not a reason to drop the email - `_extract` will try again and
        report what it found on its own line. Whatever else fails in here fails
        again inside `run` below, where it is labelled and re-raised.
        """
        if not self._reads_attachments or self._extraction is None:
            return None
        if not message.attachments:
            return None

        try:
            if not self._triage.needs_the_model(email):
                logger.debug("The rules answer this one, so its files are left alone")
                return None
            files = await self._extraction.read(await self._parse(message))
        except Exception:
            logger.exception("Could not read the files before triage")
            return None

        logger.info(
            "Read %d file(s) before triage: %s",
            len(files),
            [f"{found.origin}={found.role.value}" for found in files],
        )
        return files

    async def _extract(
        self,
        message: EmailMessage,
        email: NormalizedEmail,
        outcome: ClassificationOutcome,
        match: RegionMatch | None,
        decision_id: str | None,
        files: list[ReadDocument] | None = None,
    ) -> FilledWorkbook | None:
        """Read the RFQ, fill a copy of the template, and journal both.

        Returns the file, because the forward carries it. Runs before the
        forward and never blocks it: an RFQ that could not be read is still an
        RFQ, and a desk waiting for it should not be kept waiting by a parser.
        Everything that went wrong is on the journal line.

        `files` is the reading the verdict was taken from, reused as it stands -
        a second reading would be N model calls spent on an answer already in
        hand. None means nothing was read before triage, and then this is where
        the files are fetched and read.
        """
        if self._extraction is None:
            return None

        branch = match.region.template_branch if match else None

        try:
            read = (
                files
                if files is not None
                else await self._extraction.read(await self._parse(message))
            )
            extracted = await self._extraction.run(
                email, read, signals=outcome.hints.signals
            )
        except Exception:
            logger.exception("Could not read this RFQ")
            return None

        payload = extracted.journal_payload()
        # What the models cost, added by the handler for the same reason the
        # workbook is: the journal must not learn what an extraction is, and
        # the extraction has no idea a model was billed. Everything up to here
        # is in it - triage, one call per file, the header - and nothing after
        # here asks a model anything.
        if (spent := tally()) is not None:
            payload["spend"] = {
                "calls": spent.calls,
                "input_tokens": spent.input_tokens,
                "output_tokens": spent.output_tokens,
                "ms": spent.ms,
            }

        workbook = await self._fill(extracted, email, branch, decision_id)
        if workbook is not None:
            payload["workbook"] = workbook.journal_payload()

        await self._decisions.record_extraction(decision_id=decision_id, payload=payload)
        return workbook

    async def _fill(
        self,
        extracted: RfqExtraction,
        email: NormalizedEmail,
        branch: str | None,
        decision_id: str | None,
    ) -> FilledWorkbook | None:
        """Write the template copy for this RFQ.

        Separately guarded from the reading above, so that a workbook that could
        not be written still leaves the journal line explaining what was read.
        The copy is kept under the decision id, which is what ties the file on
        disk to the line that describes it.
        """
        if self._workbooks is None:
            return None

        try:
            return await self._workbooks.build(
                extracted,
                branch=branch,
                received_at=email.received_at,
                keep_as=decision_id,
            )
        except Exception:
            logger.exception("Could not fill the template")
            return None

    async def _parse(self, message: EmailMessage) -> list[Document]:
        """The attachments as text, tables and images. No model, no decisions."""
        return self._loader.load(await self._download(message))

    async def _download(self, message: EmailMessage) -> list[SourceFile]:
        """The attachments' bytes, one request each.

        Everything but a OneDrive link is fetched, **an attached email
        included**. `/$value` on one of those returns the message as raw MIME,
        which stage A already unpacks - and a forwarded "FW: RFQ" whose
        requisition sits inside the attached message is the case that made this
        worth doing: it used to arrive as one warning and no items at all.

        A file that will not download becomes an empty `SourceFile` rather than
        an exception: stage A reports one of those as a warning, the form now
        names it, and one unreachable attachment must not cost us the rest of
        the email.
        """
        files: list[SourceFile] = []

        for attachment in message.attachments:
            data = None
            # A reference attachment is a link: there are no bytes to ask for,
            # anywhere, and asking spends a request to be told so.
            if attachment.id and not attachment.is_reference:
                try:
                    data = await self._mailbox.get_attachment_bytes(message.id, attachment.id)
                except Exception as error:
                    # The message, not the stack. Graph refusing a download is
                    # an answer from somewhere else, and its own text says
                    # everything a traceback through our code would.
                    logger.warning("Could not download %s: %s", attachment.name, error)
            files.append(_source_file(attachment, data))

        return files

    def _already_handled(self, message: EmailMessage) -> bool:
        """Our own label on the message means this one already ran to the end.

        Graph re-sends notifications and the in-memory dedupe does not survive a
        restart. A repeated label is harmless; a repeated forward is a second
        real email, so the check lives here rather than only in the caller.
        """
        return bool(set(message.categories) & self._categories.all_names())

    async def _deliver(
        self,
        message_id: str,
        match: RegionMatch | None,
        decision_id: str | None,
        outcome: ClassificationOutcome,
        workbook: FilledWorkbook | None = None,
    ) -> Delivery:
        """Send the RFQ to its regional desk, reporting why if it does not go."""
        if not self._forward_enabled:
            return Delivery(DeliveryOutcome.DISABLED)
        if outcome.result.needs_human_review:
            # Forwarding it as well would leave a reviewer looking at mail that
            # has already gone, which they would then send a second time.
            return Delivery(DeliveryOutcome.UNSURE)

        if match is None:
            logger.warning("No single region matched, not sent")
            return Delivery(DeliveryOutcome.NO_REGION)
        if not match.region.forward_to:
            logger.warning("Region %s has no address configured, not sent", match.key)
            return Delivery(DeliveryOutcome.NO_ADDRESS, region=match.key, region_rule=match.rule)

        carrying = workbook if self._forward_workbook else None
        sent = await self._send(message_id, match, decision_id, carrying)

        if sent is None and carrying is not None:
            # The file is what failed, not the forward. Sending the RFQ without
            # it beats not sending the RFQ: the desk can quote from the
            # customer's own attachments, and the copy is still on disk.
            logger.warning("Sending again without the form")
            sent = await self._send(message_id, match, decision_id, None, lost=workbook)

        if sent is None:
            return Delivery(DeliveryOutcome.FAILED, region=match.key, region_rule=match.rule)

        logger.info(
            "Forwarded to %s, cc %s | region %s by %s | form %s",
            match.region.forward_to,
            match.region.cc,
            match.key,
            match.rule,
            sent or "not attached",
        )
        return Delivery(
            DeliveryOutcome.SENT,
            forwarded_to=match.region.forward_to,
            cc=list(match.region.cc),
            region=match.key,
            region_rule=match.rule,
            attached=sent or None,
        )

    async def _send(
        self,
        message_id: str,
        match: RegionMatch,
        decision_id: str | None,
        workbook: FilledWorkbook | None,
        lost: FilledWorkbook | None = None,
    ) -> str | None:
        """One attempt. Returns the attached filename, `""` for none, None for
        a failure the caller may want to retry differently."""
        try:
            await self._mailbox.forward(
                message_id,
                to=match.region.forward_to,
                cc=match.region.cc,
                comment=_note(workbook or lost, attached=workbook is not None,
                              decision_id=decision_id),
                attachment=_outgoing(workbook),
            )
        except Exception as error:
            # Retryable - the caller sends again without the form - and
            # Exchange refusing 12.5 MB says so in one line. Anything that
            # escapes still reaches `NotificationService` with its traceback.
            logger.warning("The forward failed: %s", error)
            return None

        return workbook.filename if workbook else ""

    def _region(
        self, email: NormalizedEmail, outcome: ClassificationOutcome
    ) -> RegionMatch | None:
        """The one desk this email belongs to, or None when it must not be guessed.

        Subject and newest message both go in - the region appears in either.
        Quoted history does not: an older message about another port would route
        this one to the wrong desk.
        """
        return resolve_region(
            self._regions,
            region_hint=email.region_hint,
            delivery_port=outcome.result.extracted.delivery_port,
            text=f"{email.subject or ''}\n{outcome.thread.latest_message}",
            mailbox=self._mailbox.address,
        )

    async def _journal(
        self,
        decision_id: str | None,
        delivery: Delivery,
        labels: list[str],
        labelled: bool,
    ) -> None:
        await self._decisions.record_delivery(
            decision_id=decision_id,
            outcome=delivery.outcome,
            forwarded_to=delivery.forwarded_to,
            cc=delivery.cc,
            region=delivery.region,
            region_rule=delivery.region_rule,
            labels=labels,
            labelled=labelled,
            attached=delivery.attached,
        )

    async def _label(self, message_id: str, names: list[str]) -> bool:
        """Stamp the message, and say whether it took.

        A label that will not stick must not lose a decision already
        journalled - but it must not be reported as applied either. This label
        is what a person sees in Outlook **and** what stops the same email
        being forwarded twice after a restart, so the journal has to know that
        neither of those is in place.
        """
        if not names:
            return True

        try:
            await self._mailbox.set_categories(message_id, names)
        except Exception as error:
            logger.warning(
                "Could not label with %s: %s - this message is not deduped either",
                names,
                error,
            )
            return False
        return True


def _filled(workbook: FilledWorkbook | None) -> str:
    """What came out of the RFQ, or why nothing did.

    On the closing line because that is where somebody scanning a run of ten
    emails looks: "0 item(s)" against a forwarded RFQ is the failure that
    otherwise reads exactly like a success.
    """
    if workbook is None:
        return "no form"
    return (
        f"{workbook.items} item(s), "
        f"{len(workbook.missing_required)} starred cell(s) blank"
    )


def _attached(message: EmailMessage) -> str:
    """The files as they arrived, by name. Names lie about type, and that is
    the point: what stage A made of them is the next line."""
    if not message.attachments:
        return "no attachments"

    names = [item.name or "unnamed" for item in message.attachments]
    shown = ", ".join(names[:MAX_NAMES_LOGGED])
    if len(names) > MAX_NAMES_LOGGED:
        shown += f" and {len(names) - MAX_NAMES_LOGGED} more"
    return f"{len(names)} attachment(s): {shown}"


def _source_file(attachment: Attachment, data: bytes | None) -> SourceFile:
    return SourceFile(
        filename=attachment.name or "attachment",
        data=data,
        content_type=attachment.content_type,
        size_bytes=attachment.size or (len(data) if data else 0),
        is_inline=attachment.is_inline,
        is_reference=attachment.is_reference,
    )


def _outgoing(workbook: FilledWorkbook | None) -> OutgoingFile | None:
    if workbook is None:
        return None
    return OutgoingFile(workbook.filename, workbook.data, CONTENT_TYPE)


def _note(
    workbook: FilledWorkbook | None, *, attached: bool, decision_id: str | None
) -> str:
    """The block above the forwarded email, as HTML.

    Empty when there is no form to talk about, which is the same forward Phase 1
    has always sent. Escaped as a whole, because none of the wording is markup
    and a vessel name with an `&` in it should not break the layout -
    `quote=False` because this is element content, where an apostrophe is an
    apostrophe rather than `&#x27;`.
    """
    if workbook is None:
        return ""

    lines = [
        ATTACHED.format(filename=workbook.filename, items=workbook.items)
        if attached
        else NOT_ATTACHED.format(decision_id=decision_id or "unknown")
    ]
    if not workbook.items:
        lines.append(NOTHING_READ)
    if workbook.unread:
        lines.append(
            NOT_READ.format(count=len(workbook.unread), files="; ".join(workbook.unread))
        )
    if workbook.conflicts:
        lines.append(DISAGREE.format(items="; ".join(workbook.conflicts)))
    if workbook.why_blank:
        # The reason beside each cell, not just its name. "IMO Number" sends
        # somebody hunting through the email again; "IMO Number: not stated
        # anywhere, the vessel is named but never numbered" does not.
        lines.append(STILL_BLANK)
        lines += [f"- {entry}" for entry in workbook.why_blank[:MAX_BLANKS_LISTED]]
        if len(workbook.why_blank) > MAX_BLANKS_LISTED:
            left = len(workbook.why_blank) - MAX_BLANKS_LISTED
            lines.append(f"- and {left} more, in the block under the form")
    lines.append(CAVEAT)

    return "<br>".join(escape(line, quote=False) for line in lines)
