"""What happens to an email once it has been fetched from the mailbox."""

import logging

from src.domain.rules.registries import OutlookCategories
from src.infrastructure.outlook.mailbox import MailboxReader
from src.infrastructure.outlook.mapping import to_normalized_email
from src.infrastructure.outlook.schemas import EmailMessage
from src.services.triage import EmailTriage

logger = logging.getLogger(__name__)


class ClassifyingEmailHandler:
    """Runs a mailbox email through the same triage the HTTP endpoint uses, then
    stamps the verdict back onto the message as an Outlook category."""

    def __init__(
        self,
        triage: EmailTriage,
        mailbox: MailboxReader,
        categories: OutlookCategories,
    ) -> None:
        self._triage = triage
        self._mailbox = mailbox
        self._categories = categories

    async def handle(self, message: EmailMessage) -> None:
        """Classify, then label. Errors get a label too and are re-raised.

        `NotificationService` logs and moves on, so one unclassifiable email
        never stops the ones behind it.
        """
        email = to_normalized_email(message, mailbox=self._mailbox.address)

        try:
            triaged = await self._triage.run(email, source="outlook")
        except Exception:
            await self._label(message.id, self._categories.for_failure())
            raise

        await self._label(message.id, self._categories.for_result(triaged.outcome.result))

    async def _label(self, message_id: str, names: list[str]) -> None:
        """A label that will not stick must not lose a decision already journalled."""
        if not names:
            return

        try:
            await self._mailbox.set_categories(message_id, names)
        except Exception:
            logger.warning("Could not label %s with %s", message_id, names, exc_info=True)
