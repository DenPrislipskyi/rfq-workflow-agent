from fastapi import APIRouter

from src.api import classify, health_check, webhooks

api_router = APIRouter()
api_router.include_router(health_check.router)
api_router.include_router(webhooks.router)
api_router.include_router(classify.router)
