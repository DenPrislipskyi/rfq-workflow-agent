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
from src.infrastructure.catalog import PublishedSheet, Snapshot
from src.infrastructure.excel import Template
from src.infrastructure.llm.registry import LLMRegistry, ModelSpec
from src.infrastructure.outlook.auth import GraphTokenProvider
from src.infrastructure.outlook.client import GraphClient
from src.infrastructure.outlook.mailbox import Mailbox
from src.infrastructure.outlook.subscription import SubscriptionManager
from src.infrastructure.storage.changes import Changes
from src.infrastructure.storage.decisions import DecisionLog
from src.core.database import _async_session_factory
from src.infrastructure.blobs import AzureBlobs, Blobs, FolderBlobs
from src.infrastructure.storage.database import DatabaseRecords
from src.infrastructure.storage.protocol import Records
from src.infrastructure.storage.records import EmailRecords
from src.infrastructure.storage.workbooks import WorkbookStore
from src.services.catalog import CatalogService
from src.services.classification.pipeline import ClassificationPipeline
from src.services.extraction import ExtractionPipeline, FileReader, HeaderReader
from src.services.handlers import ClassifyingEmailHandler
from src.services.matching import LineDescriber, MatchingPipeline
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
    records: Records
    changes: Changes
    catalog: CatalogService


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

    # One folder per email, and the only thing the front end reads. Shared the
    # same way the journal is: triage opens the record, the handler fills in
    # what became of the email, the HTTP layer reads it back.
    # The store writes; this says so. Kept apart because they are two jobs:
    # one owns the folder, the other owns "an open page should look again".
    changes = Changes()
    records = build_records(settings, changes)

    triage = EmailTriage(
        pipeline=pipeline,
        decisions=decisions,
        prompt_version=settings.PROMPT_VERSION,
        records=records,
    )

    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT_SECONDS
    ) as http_client:
        # Indexed before anything is served, so the first email of the day is
        # matched against the last good copy rather than against nothing. The
        # sheet itself is fetched afterwards, behind the running server.
        catalog = build_catalog(settings, http_client)
        catalog.load()
        matching = build_matching(settings, llms)

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
                records=records,
                matching=matching,
                catalog=lambda: catalog.current,
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

        await catalog.start()

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
                records=records,
                changes=changes,
                catalog=catalog,
            )
        finally:
            await catalog.stop()
            # The blob client holds an aiohttp session and a connector. Left
            # open, they outlive the app and say so on the way out.
            closing = getattr(records, "aclose", None)
            if closing is not None:
                await closing()
            if settings.OUTLOOK_ENABLED:
                await subscriptions.stop()


def build_matching(settings: Settings, llms: LLMRegistry) -> MatchingPipeline | None:
    """Stage D, or None when it is switched off.

    The text model for both calls: restating a line and choosing between five
    descriptions are questions about words, and neither of them opens a file.
    """
    if not settings.MATCHING_ENABLED:
        logger.info("MATCHING_ENABLED=false - RFQ lines are not looked up in the catalogue")
        return None

    return MatchingPipeline(
        LineDescriber(llms.text),
        candidates=settings.CATALOG_SHORTLIST,
        agreement=settings.MATCHING_AGREEMENT_PERCENT,
    )


def build_records(settings: Settings, changes: Changes) -> Records:
    """Where a record goes: a folder on disk, or Postgres with a container.

    Both satisfy the same protocol, so nothing above this line knows which one
    it has. The folder is the default because it needs nothing configured - a
    checkout with an empty `.env` runs, and so does the test suite.
    """
    if settings.RECORDS_STORE != "postgres":
        logger.info("Records are kept in %s", settings.DATABASE_PATH)
        return EmailRecords(
            settings.DATABASE_PATH,
            enabled=settings.DATABASE_ENABLED,
            keep_attachments=settings.DATABASE_KEEP_ATTACHMENTS,
            changes=changes,
        )

    blobs = build_blobs(settings)
    logger.info(
        "Records go to Postgres, files to %s",
        "the container " + settings.AZURE_STORAGE_CONTAINER
        if settings.AZURE_STORAGE_CONNECTION_STRING
        else settings.BLOB_FOLDER_PATH,
    )
    return DatabaseRecords(
        _async_session_factory(),
        blobs,
        enabled=settings.DATABASE_ENABLED,
        keep_attachments=settings.DATABASE_KEEP_ATTACHMENTS,
        changes=changes,
    )


def build_blobs(settings: Settings) -> Blobs:
    """The container, or a folder standing in for it.

    No connection string is not an error: a local Postgres with the files on
    disk beside it is how this is developed, and the two answer identically.
    """
    if settings.AZURE_STORAGE_CONNECTION_STRING is None:
        return FolderBlobs(settings.BLOB_FOLDER_PATH)
    return AzureBlobs(
        settings.AZURE_STORAGE_CONNECTION_STRING.get_secret_value(),
        settings.AZURE_STORAGE_CONTAINER,
    )


def build_catalog(settings: Settings, http_client: httpx.AsyncClient) -> CatalogService:
    """Our product list, from the desk's sheet through a snapshot on disk.

    No sheet id configured is not an error: the snapshot is then the whole
    story, which is how a machine with no access to the sheet still runs.
    """
    sheet = (
        PublishedSheet(http_client, settings.CATALOG_SHEET_ID, settings.CATALOG_SHEET_GID)
        if settings.CATALOG_SHEET_ID
        else None
    )
    return CatalogService(
        sheet,
        Snapshot(settings.CATALOG_SNAPSHOT_PATH),
        code_column=settings.CATALOG_CODE_COLUMN,
        description_column=settings.CATALOG_SHOWN_COLUMN,
        customer_code_column=settings.CATALOG_CUSTOMER_CODE_COLUMN,
        customer_description_column=settings.CATALOG_SEARCH_COLUMN,
        refresh_minutes=settings.CATALOG_REFRESH_MINUTES,
    )


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
