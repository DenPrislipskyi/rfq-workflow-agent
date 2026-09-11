from typing import Annotated

from fastapi import Depends, Request

from src.core.config import Settings, get_settings
from src.infrastructure.llm.registry import LLMRegistry
from src.infrastructure.outlook.subscription import SubscriptionManager
from src.infrastructure.storage.changes import Changes
from src.infrastructure.storage.records import EmailRecords
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


def get_records(request: Request) -> EmailRecords:
    return request.state.records


def get_changes(request: Request) -> Changes:
    return request.state.changes


def get_catalog(request: Request) -> CatalogService:
    return request.state.catalog


SettingsDep = Annotated[Settings, Depends(get_settings)]
TriageDep = Annotated[EmailTriage, Depends(get_triage)]
LLMRegistryDep = Annotated[LLMRegistry, Depends(get_llm_registry)]
NotificationServiceDep = Annotated[
    NotificationService, Depends(get_notification_service)
]
SubscriptionManagerDep = Annotated[
    SubscriptionManager, Depends(get_subscription_manager)
]
RecordsDep = Annotated[EmailRecords, Depends(get_records)]
ChangesDep = Annotated[Changes, Depends(get_changes)]
CatalogDep = Annotated[CatalogService, Depends(get_catalog)]
