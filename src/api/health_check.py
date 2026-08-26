from fastapi import APIRouter

from src.api.dependencies import SettingsDep, SubscriptionManagerDep

router = APIRouter(tags=["Health check"])


@router.get("/health-check", status_code=200)
async def health_check() -> dict:
    return {"status": "ok"}
