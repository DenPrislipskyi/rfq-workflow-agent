from fastapi import FastAPI

from src import register_exceptions, register_routers
from src.core.config import get_settings
from src.core.lifespan import lifespan
from src.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

    register_exceptions(app)
    register_routers(app)

    return app


app = create_app()
