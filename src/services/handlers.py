"""What happens to an email once it has been fetched from the mailbox."""

import logging
from dataclasses import dataclass, field

from src.domain.enums import DeliveryOutcome, RecommendedAction
from src.domain.models import ClassificationOutcome, NormalizedEmail
from src.domain.rules.regions import resolve_region
from src.domain.rules.registries import Registries
from src.infrastructure.outlook.mailbox import Mailbox
from src.infrastructure.outlook.mapping import to_normalized_email
from src.infrastructure.outlook.schemas import EmailMessage
from src.infrastructure.storage.decisions import DecisionLog
from src.services.triage import EmailTriage

logger = logging.getLogger(__name__)

# The three outcomes that mean "we tried and could not". A switch left off, or a
# verdict a person still has to confirm, is not a failed delivery.
UNDELIVERED = frozenset(
    {DeliveryOutcome.NO_REGION, DeliveryOutcome.NO_ADDRESS, DeliveryOutcome.FAILED}
)


@dataclass(frozen=True, slots=True)
class Delivery:
    """Where an RFQ went, and by which rule it was routed there."""

    outcome: DeliveryOutcome
    forwarded_to: str | None = None
    cc: list[str] = field(default_factory=list)
    region: str | None = None
    region_rule: str | None = None


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
    ) -> None:
        self._triage = triage
        self._mailbox = mailbox
        self._categories = registries.outlook_categories
        self._regions = registries.regions
        self._decisions = decisions
        self._forward_enabled = forward_enabled

    async def handle(self, message: EmailMessage) -> None:
        """Classify, deliver if it is an RFQ, label, and journal what happened.

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
            # No decision id yet - the classification is what failed.
            await self._label(message.id, self._categories.for_failure())
            raise

        outcome = triaged.outcome
        labels = self._categories.for_result(outcome.result)
        delivery = None

        if outcome.result.recommended_action is RecommendedAction.FORWARD_TO_DST:
            delivery = await self._deliver(message.id, email, outcome, triaged.decision_id)
            if delivery.outcome in UNDELIVERED:
                labels = self._categories.plus_not_sent(labels)

        await self._label(message.id, labels, triaged.decision_id)

        if delivery is not None:
            await self._journal(triaged.decision_id, delivery, labels)

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
        email: NormalizedEmail,
        outcome: ClassificationOutcome,
        decision_id: str | None,
    ) -> Delivery:
        """Send the RFQ to its regional desk, reporting why if it does not go."""
        if not self._forward_enabled:
            return Delivery(DeliveryOutcome.DISABLED)
        if outcome.result.needs_human_review:
            # Forwarding it as well would leave a reviewer looking at mail that
            # has already gone, which they would then send a second time.
            return Delivery(DeliveryOutcome.UNSURE)

        match = self._region(email, outcome)
        if match is None:
            logger.warning("No single region matched %s, not sent | %s", message_id, decision_id)
            return Delivery(DeliveryOutcome.NO_REGION)
        if not match.region.forward_to:
            logger.warning("Region %s has no address configured, not sent", match.key)
            return Delivery(DeliveryOutcome.NO_ADDRESS, region=match.key, region_rule=match.rule)

        try:
            await self._mailbox.forward(
                message_id, to=match.region.forward_to, cc=match.region.cc
            )
        except Exception:
            logger.exception("Forward of %s failed | %s", message_id, decision_id)
            return Delivery(DeliveryOutcome.FAILED, region=match.key, region_rule=match.rule)

        logger.info(
            "Forwarded %s to %s, cc %s | region %s by %s | %s",
            message_id,
            match.region.forward_to,
            match.region.cc,
            match.key,
            match.rule,
            decision_id,
        )
        return Delivery(
            DeliveryOutcome.SENT,
            forwarded_to=match.region.forward_to,
            cc=list(match.region.cc),
            region=match.key,
            region_rule=match.rule,
        )

    def _region(self, email: NormalizedEmail, outcome: ClassificationOutcome):
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
        self, decision_id: str | None, delivery: Delivery, labels: list[str]
    ) -> None:
        await self._decisions.record_delivery(
            decision_id=decision_id,
            outcome=delivery.outcome,
            forwarded_to=delivery.forwarded_to,
            cc=delivery.cc,
            region=delivery.region,
            region_rule=delivery.region_rule,
            labels=labels,
        )

    async def _label(
        self, message_id: str, names: list[str], decision_id: str | None = None
    ) -> None:
        """A label that will not stick must not lose a decision already journalled."""
        if not names:
            return

        try:
            await self._mailbox.set_categories(message_id, names)
        except Exception:
            logger.warning(
                "Could not label %s with %s | %s",
                message_id,
                names,
                decision_id,
                exc_info=True,
            )
