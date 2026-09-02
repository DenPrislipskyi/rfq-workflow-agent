"""What happens to an email once it has been fetched from the mailbox."""

import logging

from src.domain.enums import RecommendedAction
from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.domain.rules.regions import Region, resolve_region
from src.domain.rules.registries import Registries
from src.infrastructure.outlook.mailbox import Mailbox
from src.infrastructure.outlook.mapping import to_normalized_email
from src.infrastructure.outlook.schemas import EmailMessage
from src.services.triage import EmailTriage

logger = logging.getLogger(__name__)


class ClassifyingEmailHandler:
    """Runs a mailbox email through the same triage the HTTP endpoint uses, then
    acts on the verdict: forwards the RFQs and labels every message."""

    def __init__(
        self,
        triage: EmailTriage,
        mailbox: Mailbox,
        registries: Registries,
        *,
        forward_enabled: bool = False,
    ) -> None:
        self._triage = triage
        self._mailbox = mailbox
        self._categories = registries.outlook_categories
        self._regions = registries.regions
        self._forward_enabled = forward_enabled

    async def handle(self, message: EmailMessage) -> None:
        """Classify, forward if it is an RFQ, then label.

        Forwarding runs before labelling on purpose: crashing in between then
        risks a duplicate rather than a silent loss, and by this project's own
        arithmetic a duplicate costs an operator seconds while a lost RFQ costs
        a sale. A failed classification is labelled too, and re-raised for
        `NotificationService` to log - one bad email must not stop the queue.
        """
        if self._already_handled(message):
            logger.info("Message %s is already labelled, skipping", message.id)
            return

        email = to_normalized_email(message, mailbox=self._mailbox.address)

        try:
            triaged = await self._triage.run(email, source="outlook")
        except Exception:
            await self._label(message.id, self._categories.for_failure())
            raise

        outcome = triaged.outcome
        labels = self._categories.for_result(outcome.result)

        if self._forwards(outcome) and not await self._forward(message.id, email, outcome):
            labels = self._categories.plus_not_sent(labels)

        await self._label(message.id, labels)

    def _already_handled(self, message: EmailMessage) -> bool:
        """Our own label on the message means this one already ran to the end.

        Graph re-sends notifications and the in-memory dedupe does not survive a
        restart. A repeated label is harmless; a repeated forward is a second
        real email, so the check lives here rather than only in the caller.
        """
        return bool(set(message.categories) & self._categories.all_names())

    def _forwards(self, outcome: ClassificationOutcome) -> bool:
        """Only a confident RFQ is sent on its own.

        An email the agent wants a person to check is labelled for review, and
        forwarding it as well would leave a reviewer looking at mail that has
        already gone - which they would then send a second time.
        """
        return (
            self._forward_enabled
            and outcome.result.recommended_action is RecommendedAction.FORWARD_TO_DST
            and not outcome.result.needs_human_review
        )

    async def _forward(
        self, message_id: str, email: NormalizedEmail, outcome: ClassificationOutcome
    ) -> bool:
        """Send the RFQ to its desk. False means a person has to route it instead."""
        region = self._desk_for(email, outcome)
        if region is None:
            logger.warning("Message %s has no routable region, not sent", message_id)
            return False

        try:
            await self._mailbox.forward(message_id, to=region.forward_to, cc=region.cc)
        except Exception:
            logger.exception("Message %s could not be forwarded", message_id)
            return False

        logger.info("Forwarded %s to %s, cc %s", message_id, region.forward_to, region.cc)
        return True

    def _desk_for(
        self, email: NormalizedEmail, outcome: ClassificationOutcome
    ) -> Region | None:
        """The one desk this email belongs to, or None when it must not be guessed.

        Subject and newest message both go in - the region appears in either.
        Quoted history does not: an older message about another port would route
        this one to the wrong desk.
        """
        match = resolve_region(
            self._regions,
            region_hint=email.region_hint,
            delivery_port=outcome.result.extracted.delivery_port,
            text=f"{email.subject or ''}\n{outcome.thread.latest_message}",
            mailbox=self._mailbox.address,
        )
        if match is None:
            return None

        logger.debug("Region %s matched by %s", match.key, match.rule)
        return match.region if match.region.forward_to else None

    async def _label(self, message_id: str, names: list[str]) -> None:
        """A label that will not stick must not lose a decision already journalled."""
        if not names:
            return

        try:
            await self._mailbox.set_categories(message_id, names)
        except Exception:
            logger.warning("Could not label %s with %s", message_id, names, exc_info=True)
