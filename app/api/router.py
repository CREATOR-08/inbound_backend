from fastapi import APIRouter

from app.api.routes.calendar import action_router as calendar_action_router
from app.api.routes.emails import action_router as email_action_router
from app.api.routes.emails import router as emails_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(emails_router)
api_router.include_router(email_action_router, prefix="/actions")
api_router.include_router(calendar_action_router, prefix="/actions")
