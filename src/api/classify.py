import logging

from fastapi import APIRouter, status

from src.api.dependencies import LLMRegistryDep, SettingsDep, TriageDep
from src.api.schemas import ClassifyEmailRequest, ClassifyEmailResponse
from src.core.exceptions import (
    LLMTimeoutException,
    LLMUnavailableException,
    UnknownProviderException,
)
from src.infrastructure.llm.exceptions import LLMError, LLMTimeoutError
from src.infrastructure.llm.registry import UnknownProviderError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/emails", tags=["Classification"])


@router.post("/classify", status_code=status.HTTP_200_OK)
async def classify_email(
    request: ClassifyEmailRequest,
    triage: TriageDep,
    llms: LLMRegistryDep,
    settings: SettingsDep,
) -> ClassifyEmailResponse:
    """Classify one email. Accepts a pasted dump or an already parsed message.

    The same triage the Outlook handler uses, only reached over HTTP - except
    that a caller may name a model, which is how two models are compared on one
    corpus without a config change. The response reports which one answered.
    """
    email = request.to_domain()

    try:
        override = (
            llms.override(provider=request.provider, model=request.model)
            if request.model or request.provider
            else None
        )
    except UnknownProviderError as error:
        raise UnknownProviderException(str(error)) from error

    try:
        triaged = await triage.run(email, source="http", llm=override)
    except LLMTimeoutError as error:
        raise LLMTimeoutException(str(error)) from error
    except LLMError as error:
        logger.warning("Classification failed: %s", error)
        raise LLMUnavailableException(str(error)) from error

    return ClassifyEmailResponse.from_outcome(
        triaged.outcome, email, settings.PROMPT_VERSION, triaged.decision_id
    )
