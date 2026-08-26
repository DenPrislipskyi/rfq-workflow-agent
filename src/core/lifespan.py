import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypedDict

import httpx
from fastapi import FastAPI

from src.core.config import Settings, get_settings
from src.domain.rules.registries import Registries
from src.infrastructure.llm.client import StructuredLLM
from src.infrastructure.outlook.auth import GraphTokenProvider
from src.infrastructure.outlook.client import GraphClient
from src.infrastructure.outlook.mailbox import MailboxReader
from src.infrastructure.outlook.subscription import SubscriptionManager
from src.infrastructure.storage.decisions import DecisionLog
from src.services.classification.pipeline import ClassificationPipeline
from src.services.handlers import ClassifyingEmailHandler
from src.services.notification_service import NotificationService
from src.services.triage import EmailTriage

logger = logging.getLogger(__name__)


class LifespanState(TypedDict):
    """Objects Starlette copies into every request's state."""

    triage: EmailTriage
    notification_service: NotificationService
    subscription_manager: SubscriptionManager


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[LifespanState]:
    """Builds every service, keeps it alive while the app serves, tears it down."""
    settings = get_settings()

    # Read once at boot: a malformed registry file must break startup, not the
    # first email of the day.
    registries = Registries.load(settings.REGISTRIES_PATH, mailbox=settings.MAILBOX_ADDRESS)
    pipeline = ClassificationPipeline(
        llm=build_llm(settings),
        registries=registries,
        settings=settings,
    )
    logger.info("Classifier ready: %s via %s", settings.LLM_MODEL, settings.LLM_PROVIDER)

    triage = EmailTriage(
        pipeline=pipeline,
        decisions=DecisionLog(
            settings.DECISIONS_LOG_PATH,
            enabled=settings.PERSIST_DECISIONS,
            log_bodies=settings.LOG_EMAIL_BODIES,
        ),
        prompt_version=settings.PROMPT_VERSION,
    )

    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT_SECONDS
    ) as http_client:
        graph_client = GraphClient(
            http_client=http_client,
            token_provider=GraphTokenProvider(
                client_id=settings.APPLICATION_CLIENT_ID,
                client_secret=settings.CLIENT_SECRET_VALUE,
                authority=settings.authority_url,
            ),
            base_url=settings.GRAPH_BASE_URL,
        )
        mailbox = MailboxReader(graph_client, settings.MAILBOX_ADDRESS)
        notification_service = NotificationService(
            mailbox=mailbox,
            handler=ClassifyingEmailHandler(
                triage, mailbox, registries.outlook_categories
            ),
            client_state=settings.WEBHOOK_CLIENT_STATE,
        )
        subscriptions = SubscriptionManager(
            client=graph_client,
            resource=mailbox.inbox_resource,
            notification_url=settings.notification_url,
            client_state=settings.WEBHOOK_CLIENT_STATE,
            expiration_minutes=settings.SUBSCRIPTION_EXPIRATION_MINUTES,
            renewal_margin_minutes=settings.SUBSCRIPTION_RENEWAL_MARGIN_MINUTES,
        )

        if settings.OUTLOOK_ENABLED:
            await subscriptions.start()
            logger.info(
                "Watching %s, notifications go to %s",
                settings.MAILBOX_ADDRESS,
                settings.notification_url,
            )
        else:
            logger.info("OUTLOOK_ENABLED=false - the mailbox is not watched, HTTP only")

        try:
            yield LifespanState(
                triage=triage,
                notification_service=notification_service,
                subscription_manager=subscriptions,
            )
        finally:
            if settings.OUTLOOK_ENABLED:
                await subscriptions.stop()


def build_llm(settings: Settings) -> StructuredLLM:
    """Every provider-specific value comes from Settings, none from the code."""
    return StructuredLLM(
        provider=settings.LLM_PROVIDER,
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY.get_secret_value(),
        timeout_s=settings.LLM_TIMEOUT_S,
        max_retries=settings.LLM_MAX_RETRIES,
        structured_output_method=settings.LLM_STRUCTURED_OUTPUT_METHOD,
        temperature=settings.LLM_TEMPERATURE,
        max_tokens=settings.LLM_MAX_TOKENS,
        extra_options=settings.LLM_EXTRA_OPTIONS,
    )
