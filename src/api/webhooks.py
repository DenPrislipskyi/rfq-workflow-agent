import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from src.api.dependencies import NotificationServiceDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])


@router.post("/webhooks/outlook", status_code=status.HTTP_202_ACCEPTED)
async def outlook_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    service: NotificationServiceDep,
    validation_token: Annotated[str | None, Query(alias="validationToken")] = None,
) -> Response:
    # Graph proves ownership of the endpoint before creating a subscription:
    # the token must come back as plain text with nothing added.
    if validation_token is not None:
        logger.info("Validation request from Microsoft Graph")
        return PlainTextResponse(validation_token)

    message_ids = service.accept(await request.json())

    # Graph expects an answer within seconds, so fetching happens afterwards.
    if message_ids:
        background_tasks.add_task(service.process, message_ids)

    return Response(status_code=status.HTTP_202_ACCEPTED)
