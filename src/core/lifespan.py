import logging
from collections.abc import AsyncIterator
from datetime import UTC, tzinfo
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from typing import TypedDict

import httpx
from fastapi import FastAPI

from src.core.config import Settings, get_settings
from src.domain.rules.registries import Registries
from src.infrastructure.excel import Template
from src.infrastructure.llm.registry import LLMRegistry, ModelSpec
from src.infrastructure.outlook.auth import GraphTokenProvider
from src.infrastructure.outlook.client import GraphClient
from src.infrastructure.outlook.mailbox import Mailbox
from src.infrastructure.outlook.subscription import SubscriptionManager
from src.infrastructure.storage.decisions import DecisionLog
from src.infrastructure.storage.workbooks import WorkbookStore
from src.services.classification.pipeline import ClassificationPipeline
from src.services.extraction import ExtractionPipeline, FileReader, HeaderReader
from src.services.handlers import ClassifyingEmailHandler
from src.services.notification_service import NotificationService
from src.services.triage import EmailTriage
from src.services.workbook import WorkbookBuilder

logger = logging.getLogger(__name__)


class LifespanState(TypedDict):
    """Objects Starlette copies into every request's state."""

    triage: EmailTriage
    llms: LLMRegistry
    notification_service: NotificationService
    subscription_manager: SubscriptionManager


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[LifespanState]:
    """Builds every service, keeps it alive while the app serves, tears it down."""
    settings = get_settings()

    # Read once at boot: a malformed registry file must break startup, not the
    # first email of the day.
    registries = Registries.load(
        settings.REGISTRIES_PATH,
        mailbox=settings.MAILBOX_ADDRESS,
        region_mailboxes=settings.region_mailboxes,
        region_cc=settings.region_cc,
    )
    llms = build_llm_registry(settings)
    pipeline = ClassificationPipeline(
        llm=llms.text,
        registries=registries,
        settings=settings,
    )
    for purpose, model in llms.describe().items():
        logger.info("Model for %-9s %s", purpose, model)

    extraction = build_extraction(settings, llms)
    workbooks = build_workbooks(settings)

    # One journal, shared: triage writes the verdict, the handler writes what
    # became of it, and the two lines are tied by the same decision id.
    decisions = DecisionLog(
        settings.DECISIONS_LOG_PATH,
        enabled=settings.PERSIST_DECISIONS,
        log_bodies=settings.LOG_EMAIL_BODIES,
    )

    triage = EmailTriage(
        pipeline=pipeline,
        decisions=decisions,
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
        mailbox = Mailbox(graph_client, settings.MAILBOX_ADDRESS)
        notification_service = NotificationService(
            mailbox=mailbox,
            handler=ClassifyingEmailHandler(
                triage,
                mailbox,
                registries,
                decisions,
                forward_enabled=settings.FORWARD_ENABLED,
                forward_workbook=settings.FORWARD_WORKBOOK,
                reads_attachments=settings.TRIAGE_READS_ATTACHMENTS,
                extraction=extraction,
                workbooks=workbooks,
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

        if settings.FORWARD_ENABLED:
            desks = {
                key: region.forward_to or "NO ADDRESS SET"
                for key, region in registries.regions.items()
            }
            carries = "with the filled form" if settings.FORWARD_WORKBOOK else "on their own"
            logger.info("Forwarding is ON %s, RFQs go to %s", carries, desks)
        else:
            logger.info("FORWARD_ENABLED=false - RFQs are labelled but not forwarded")

        if extraction is not None and not settings.TRIAGE_READS_ATTACHMENTS:
            logger.info(
                "TRIAGE_READS_ATTACHMENTS=false - the verdict is taken from the "
                "message alone, and the files are read only once it says RFQ"
            )

        try:
            yield LifespanState(
                triage=triage,
                llms=llms,
                notification_service=notification_service,
                subscription_manager=subscriptions,
            )
        finally:
            if settings.OUTLOOK_ENABLED:
                await subscriptions.stop()


def build_extraction(settings: Settings, llms: LLMRegistry) -> ExtractionPipeline | None:
    """Phase 2, or None when it is switched off.

    Both readers work on attachments, so both take the document model; the text
    model stays with triage, the one job that reads only the message.
    """
    if not settings.EXTRACTION_ENABLED:
        logger.info("EXTRACTION_ENABLED=false - RFQs are routed but not read")
        return None

    model = llms.documents
    return ExtractionPipeline(files=FileReader(model), header=HeaderReader(model))


def build_workbooks(settings: Settings) -> WorkbookBuilder | None:
    """Stage C, or None when there is nothing to write into.

    The master is read once here and held as bytes, so that every RFQ of the day
    is cut from the same file and the checksum on each journal line says which
    file that was. A template that will not load is not fatal: reading the RFQ
    is still worth doing, and the desk still gets the email.
    """
    if not settings.EXTRACTION_ENABLED:
        return None

    try:
        template = Template.load(settings.RFQ_TEMPLATE_PATH)
    except Exception:
        logger.exception(
            "Could not load %s - RFQs will be read but no form filled",
            settings.RFQ_TEMPLATE_PATH,
        )
        return None

    return WorkbookBuilder(
        template,
        WorkbookStore(settings.WORKBOOKS_PATH, enabled=settings.WORKBOOKS_ENABLED),
        timezone=_timezone(settings.RFQ_TIMEZONE),
        remarks=settings.RFQ_REMARKS,
    )


def _timezone(name: str) -> tzinfo:
    """The zone the form's timestamps are written in, or UTC with a warning.

    A misspelt zone must not stop the service, and UTC is the honest fallback:
    it is what Graph hands us in the first place.
    """
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("RFQ_TIMEZONE=%r is not a zone I know - writing UTC", name)
        return UTC


def _master(settings: Settings) -> MasterData | None:
    """The workbook's own data, or nothing at all with a loud warning.

    A missing master must not stop the service: without the lists a port is
    left blank and reported, which is the same thing that happens when a port
    is unrecognised, and Phase 1 keeps working either way.
    """
    try:
        return load_master(settings.TEMPLATE_PATH)
    except Exception:
        logger.exception(
            "Could not read %s - the form will be filled with what the email says",
            settings.TEMPLATE_PATH,
        )
        return None


def build_llm_registry(settings: Settings) -> LLMRegistry:
    """Every provider-specific value comes from Settings, none from the code.

    The two specs differ only in provider, model and key; timeouts, retries and
    the structured-output method are properties of this service rather than of
    either question, so both purposes share them.
    """
    shared = {
        "timeout_s": settings.LLM_TIMEOUT_S,
        "max_retries": settings.LLM_MAX_RETRIES,
        "structured_output_method": settings.LLM_STRUCTURED_OUTPUT_METHOD,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "extra_options": settings.LLM_EXTRA_OPTIONS,
    }
    return LLMRegistry(
        text=ModelSpec(
            provider=settings.LLM_PROVIDER,
            model=settings.LLM_MODEL,
            api_key=settings.LLM_API_KEY.get_secret_value(),
            **shared,
        ),
        documents=ModelSpec(
            provider=settings.document_provider,
            model=settings.document_model,
            api_key=settings.document_api_key,
            **shared,
        ),
    )
