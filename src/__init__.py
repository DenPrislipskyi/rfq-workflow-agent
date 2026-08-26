from fastapi import FastAPI

from src.api.router import api_router
from src.core.exceptions import add_exceptions_handlers


def register_routers(app: FastAPI) -> None:
    app.include_router(api_router)


def register_exceptions(app: FastAPI) -> None:
    add_exceptions_handlers(app)
