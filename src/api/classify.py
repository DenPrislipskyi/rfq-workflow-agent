import logging

from fastapi import APIRouter, status

from src.api.dependencies import SettingsDep, TriageDep
from src.api.schemas import ClassifyEmailRequest, ClassifyEmailResponse
from src.core.exceptions import LLMTimeoutException, LLMUnavailableException
from src.infrastructure.llm.exceptions import LLMError, LLMTimeoutError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/emails", tags=["Classification"])


@router.post("/classify", status_code=status.HTTP_200_OK)
async def classify_email(
    request: ClassifyEmailRequest,
    triage: TriageDep,
    settings: SettingsDep,
) -> ClassifyEmailResponse:
    """Classify one email. Accepts a pasted dump or an already parsed message.

    The same triage the Outlook handler uses, only reached over HTTP.
    """
    email = request.to_domain()

    try:
        triaged = await triage.run(email, source="http")
    except LLMTimeoutError as error:
        raise LLMTimeoutException(str(error)) from error
    except LLMError as error:
        logger.warning("Classification failed: %s", error)
        raise LLMUnavailableException(str(error)) from error

    return ClassifyEmailResponse.from_outcome(
        triaged.outcome, email, settings.PROMPT_VERSION, triaged.decision_id
    )
