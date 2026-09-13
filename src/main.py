import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import register_exceptions, register_routers
from src.core.config import get_settings
from src.core.lifespan import lifespan
from src.core.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

    # The RFQ screens are served from another host, so the browser will not let
    # them call this at all unless it is named here. Added only when there is
    # something to add: an API that answers webhooks needs no browser, and a
    # wildcard on one serving customer mail is not a default worth having.
    if origins := settings.cors_origins:
        logger.info("Browser requests allowed from %s", ", ".join(origins))
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            # Named rather than wildcarded, because a wildcard and credentials
            # are mutually exclusive in every browser - and this will carry a
            # session one day.
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    register_exceptions(app)
    register_routers(app)

    return app


app = create_app()
