import logging
from collections import OrderedDict

from src.infrastructure.outlook.mailbox import Mailbox
from src.infrastructure.outlook.schemas import ChangeNotificationCollection
from src.services.handlers import ClassifyingEmailHandler

logger = logging.getLogger(__name__)


class ProcessedMessages:
    """Bounded memory of handled message ids - Graph re-sends notifications."""

    def __init__(self, capacity: int = 1000) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def register(self, message_id: str) -> bool:
        """Remember the id and report whether it was seen for the first time."""
        if message_id in self._ids:
            return False

        self._ids[message_id] = None
        if len(self._ids) > self._capacity:
            self._ids.popitem(last=False)

        return True


class NotificationService:
    """Notification -> fetched email -> handler.

    `accept` is cheap and runs inside the request, `process` does the network
    work after the response has already been sent back to Graph.
    """

    def __init__(
        self,
        mailbox: Mailbox,
        handler: ClassifyingEmailHandler,
        client_state: str,
    ) -> None:
        self._mailbox = mailbox
        self._handler = handler
        self._client_state = client_state
        self._processed = ProcessedMessages()

    def accept(self, payload: dict) -> list[str]:
        """Keep notifications that are trusted, identifiable and not seen yet."""
        notifications = ChangeNotificationCollection.model_validate(payload)
        message_ids = []

        for notification in notifications.value:
            if notification.client_state != self._client_state:
                logger.warning("Notification with an unexpected clientState dropped")
                continue

            message_id = notification.message_id
            if message_id is None:
                logger.warning("Notification without a message id dropped")
                continue

            if self._processed.register(message_id):
                message_ids.append(message_id)
            else:
                # Graph re-sends until it gets a 2xx it is happy with, so this
                # is routine. Worth seeing when an email seems to be ignored.
                logger.debug("Notification for %s seen before, dropped", message_id)

        return message_ids

    async def process(self, message_ids: list[str]) -> None:
        for message_id in message_ids:
            try:
                message = await self._mailbox.get_message(message_id)
                await self._handler.handle(message)
            except Exception:
                logger.exception("Message %s could not be processed", message_id)
