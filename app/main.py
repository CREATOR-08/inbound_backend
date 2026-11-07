import logging
import os
import uuid
from time import perf_counter

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.router import api_router
from app.db.database import (
    reset_database_request_context,
    set_database_request_context,
)

application_logger = logging.getLogger("app")
application_logger.setLevel(logging.INFO)
if not application_logger.handlers:
    application_handler = logging.StreamHandler()
    application_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    application_logger.addHandler(application_handler)
application_logger.propagate = False

app = FastAPI(title="MailFlow Backend")
logger = logging.getLogger(__name__)

_TIMED_EMAIL_ENDPOINTS = {
    "/api/v1/emails/sync-initial/status": "get_initial_sync_status",
    "/api/v1/emails/sync-initial": "sync_initial_emails",
    "/api/v1/emails": "get_emails",
}


@app.middleware("http")
async def log_email_endpoint_timing(request, call_next):
    endpoint = _TIMED_EMAIL_ENDPOINTS.get(request.url.path)
    if (
        endpoint is None
        and request.url.path.startswith("/api/v1/emails/")
        and request.url.path.endswith("/body")
    ):
        endpoint = "email_body"
    if endpoint is None:
        return await call_next(request)

    request_id = uuid.uuid4().hex
    context_token = set_database_request_context(endpoint, request_id)
    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        reset_database_request_context(context_token)
        logger.info(
            "email_endpoint_timing endpoint=%s total_endpoint_duration_ms=%.1f "
            "status_code=%s request_id=%s",
            endpoint,
            (perf_counter() - started) * 1000,
            status_code,
            request_id,
        )

frontend_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "https://inbound-opal.vercel.app",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=False,
    allow_methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],
    allow_headers=[
        "Authorization",
        "Content-Type",
    ],
)

# Register routes
app.include_router(api_router)

@app.get("/health")
def health_check():
    return {"status": "ok"}