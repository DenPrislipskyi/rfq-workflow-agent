from fastapi import APIRouter

from src.api import catalog, classify, health_check, quotes, webhooks

api_router = APIRouter()
api_router.include_router(health_check.router)
api_router.include_router(webhooks.router)
api_router.include_router(classify.router)
api_router.include_router(quotes.router)
api_router.include_router(catalog.router)
