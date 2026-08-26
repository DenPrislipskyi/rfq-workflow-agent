import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from src.infrastructure.outlook.client import GraphClient

logger = logging.getLogger(__name__)

SUBSCRIPTIONS_PATH = "/subscriptions"
RETRY_DELAY_SECONDS = 60
STARTUP_DELAY_SECONDS = 2.0


def to_graph_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class SubscriptionManager:
    """Keeps exactly one live Graph change subscription for a resource.

    Creation runs in a background task on purpose: Graph validates the
    notification URL by calling it back, which can only succeed once the HTTP
    server accepts requests - that is after startup ends.
    """

    def __init__(
        self,
        client: GraphClient,
        resource: str,
        notification_url: str,
        client_state: str,
        expiration_minutes: int,
        renewal_margin_minutes: int,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
    ) -> None:
        self._client = client
        self._resource = resource
        self._notification_url = notification_url
        self._client_state = client_state
        self._expiration_minutes = expiration_minutes
        self._renewal_margin_minutes = renewal_margin_minutes
        self._startup_delay_seconds = startup_delay_seconds

        self._subscription_id: str | None = None
        self._expires_at: datetime | None = None
        self._task: asyncio.Task | None = None

    def status(self) -> dict:
        """Current state, for health checks and debugging."""
        return {
            "subscription_id": self._subscription_id,
            "expires_at": self._expires_at,
        }

    async def start(self) -> None:
        """Return immediately; the subscription is established in the background."""
        self._task = asyncio.create_task(self._run(), name="graph-subscription")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        await self._delete_current()

    async def _run(self) -> None:
        await asyncio.sleep(self._startup_delay_seconds)
        await self._drop_stale()

        while True:
            try:
                if self._subscription_id is None:
                    await self._create()
                else:
                    await self._renew()
                delay = self._renewal_interval_seconds
            except Exception:
                logger.exception("Graph subscription is not healthy, will retry")
                self._subscription_id = None
                delay = RETRY_DELAY_SECONDS

            await asyncio.sleep(delay)

    @property
    def _renewal_interval_seconds(self) -> float:
        minutes = max(self._expiration_minutes - self._renewal_margin_minutes, 1)
        return minutes * 60

    def _next_expiration(self) -> datetime:
        return datetime.now(UTC) + timedelta(minutes=self._expiration_minutes)

    async def _create(self) -> None:
        expires_at = self._next_expiration()

        payload = await self._client.post(
            SUBSCRIPTIONS_PATH,
            {
                "changeType": "created",
                "resource": self._resource,
                "notificationUrl": self._notification_url,
                "clientState": self._client_state,
                "expirationDateTime": to_graph_timestamp(expires_at),
            },
        )

        self._subscription_id = payload["id"]
        self._expires_at = expires_at
        logger.info(
            "Subscription %s created, expires at %s", self._subscription_id, expires_at
        )

    async def _renew(self) -> None:
        expires_at = self._next_expiration()

        await self._client.patch(
            f"{SUBSCRIPTIONS_PATH}/{self._subscription_id}",
            {"expirationDateTime": to_graph_timestamp(expires_at)},
        )

        self._expires_at = expires_at
        logger.info(
            "Subscription %s renewed until %s", self._subscription_id, expires_at
        )

    async def _drop_stale(self) -> None:
        """Delete subscriptions left by previous runs.

        After a crash or an ngrok URL change Graph keeps pushing to a dead
        endpoint, and subscriptions per mailbox are limited.
        """
        try:
            payload = await self._client.get(SUBSCRIPTIONS_PATH)
            for item in payload.get("value", []):
                if item.get("resource") != self._resource:
                    continue
                await self._client.delete(f"{SUBSCRIPTIONS_PATH}/{item['id']}")
                logger.info("Stale subscription %s removed", item["id"])
        except Exception:
            logger.warning("Could not clean up stale subscriptions", exc_info=True)

    async def _delete_current(self) -> None:
        if self._subscription_id is None:
            return

        try:
            await self._client.delete(f"{SUBSCRIPTIONS_PATH}/{self._subscription_id}")
            logger.info("Subscription %s deleted", self._subscription_id)
        except Exception:
            logger.warning(
                "Could not delete subscription %s", self._subscription_id, exc_info=True
            )
        finally:
            self._subscription_id = None
            self._expires_at = None
