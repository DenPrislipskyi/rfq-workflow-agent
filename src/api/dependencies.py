from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from src.core.config import Settings, get_settings
from src.infrastructure.llm.registry import LLMRegistry
from src.infrastructure.outlook.subscription import SubscriptionManager
from src.infrastructure.storage.changes import Changes
from src.infrastructure.storage.protocol import Records
from src.services.catalog import CatalogService
from src.services.notification_service import NotificationService
from src.services.triage import EmailTriage


def get_triage(request: Request) -> EmailTriage:
    return request.state.triage


def get_llm_registry(request: Request) -> LLMRegistry:
    return request.state.llms


def get_notification_service(request: Request) -> NotificationService:
    return request.state.notification_service


def get_subscription_manager(request: Request) -> SubscriptionManager:
    return request.state.subscription_manager


def get_records(request: Request) -> Records:
    return request.state.records


def get_changes(request: Request) -> Changes:
    return request.state.changes


def get_catalog(request: Request) -> CatalogService:
    return request.state.catalog


def get_quotation_logo() -> Path | None:
    """The mark on the quotation PDF. A dependency of its own, rather than read
    off the settings where it is used, so the endpoint can be exercised without
    a whole `.env` behind it."""
    return get_settings().QUOTATION_LOGO_PATH


def get_customer_file_template() -> Path:
    """The customer's spreadsheet layout, for the same reason as the logo: the
    endpoint must be testable without a whole `.env`."""
    return get_settings().CUSTOMER_FILE_TEMPLATE_PATH


def get_quote_template() -> Path:
    """The desk's quotation workbook - a dependency for the same reason."""
    return get_settings().QUOTE_TEMPLATE_PATH


SettingsDep = Annotated[Settings, Depends(get_settings)]
QuotationLogoDep = Annotated[Path | None, Depends(get_quotation_logo)]
CustomerFileTemplateDep = Annotated[Path, Depends(get_customer_file_template)]
QuoteTemplateDep = Annotated[Path, Depends(get_quote_template)]
TriageDep = Annotated[EmailTriage, Depends(get_triage)]
LLMRegistryDep = Annotated[LLMRegistry, Depends(get_llm_registry)]
NotificationServiceDep = Annotated[
    NotificationService, Depends(get_notification_service)
]
SubscriptionManagerDep = Annotated[
    SubscriptionManager, Depends(get_subscription_manager)
]
RecordsDep = Annotated[Records, Depends(get_records)]
ChangesDep = Annotated[Changes, Depends(get_changes)]
CatalogDep = Annotated[CatalogService, Depends(get_catalog)]
