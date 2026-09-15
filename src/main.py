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
    if not (origins := settings.cors_origins):
        # Said out loud, because the alternative is silence: with no middleware
        # a preflight is answered `405 Method Not Allowed` by the router, which
        # names neither CORS nor the setting that would have fixed it.
        logger.warning(
            "CORS_ORIGINS is empty - no browser may call this API. "
            "A page trying to will see its preflight refused with 405."
        )
    elif origins == ["*"]:
        # Fine while nothing here authenticates anybody: CORS protects a user's
        # session from another site, and there is no session to protect. It
        # stops being fine the day this grows a login.
        logger.warning("Browser requests allowed from ANY origin - CORS_ORIGINS is `*`")
    else:
        logger.info("Browser requests allowed from %s", ", ".join(origins))
    if origins:
        # `*` and credentials are mutually exclusive in every browser: a
        # response carrying both is discarded, and the request looks like a
        # plain CORS failure with nothing to say why. So the wildcard turns
        # credentials off rather than producing that combination.
        wildcard = origins == ["*"]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=not wildcard,
            # Every method the router actually answers. A missing one is not a
            # 405 a reader could chase: the browser refuses the preflight, the
            # request never leaves the page, and the button looks dead. `PUT`
            # was missing when line confirmation was added, and that is exactly
            # how it presented.
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    register_exceptions(app)
    register_routers(app)

    return app


app = create_app()
