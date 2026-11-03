import hmac
import logging
import os
from time import perf_counter

from fastapi import APIRouter, Depends, Header, HTTPException
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from sqlalchemy import and_, case, func, or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.google_profile_cache import invalidate_google_profile_verification
from app.api.schemas.schemas import GoogleTokenSyncRequest
from app.db.database import get_db
from app.db.models import UserAccount
from app.services.gmail import get_gmail_service_from_token

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.put("/google-tokens")
def sync_google_tokens(
    request: GoogleTokenSyncRequest,
    sync_secret: str | None = Header(
        default=None,
        alias="X-Google-Token-Sync-Secret",
    ),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    configured_secret = os.getenv("GOOGLE_TOKEN_SYNC_SECRET")
    if not configured_secret:
        logger.error(
            "Google token sync unavailable: server shared secret is not configured"
        )
        raise HTTPException(
            status_code=503,
            detail="Google account token sync is not configured.",
        )
    if not sync_secret or not hmac.compare_digest(sync_secret, configured_secret):
        logger.warning("Google token sync rejected: invalid server credentials")
        raise HTTPException(status_code=401, detail="Invalid token sync credentials.")

    credential_setup_ms = 0.0
    profile_ms = 0.0
    try:
        credential_setup_started = perf_counter()
        try:
            service, _ = get_gmail_service_from_token(request.access_token)
        finally:
            credential_setup_ms = (
                perf_counter() - credential_setup_started
            ) * 1000

        profile_started = perf_counter()
        try:
            profile = service.users().getProfile(userId="me").execute()
        finally:
            profile_ms = (perf_counter() - profile_started) * 1000
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        if status in (401, 403):
            invalidate_google_profile_verification(request.access_token)
        logger.warning(
            "Google token sync failed during Google account verification "
            "(HTTP status=%s)",
            status,
        )
        raise HTTPException(
            status_code=401 if status == 401 else 403 if status == 403 else 502,
            detail=(
                "Google Gmail access must be reauthorized."
                if status in (401, 403)
                else "Unable to verify the Google account."
            ),
        ) from error
    except GoogleAuthError as error:
        invalidate_google_profile_verification(request.access_token)
        logger.warning(
            "Google token sync failed during Google account verification "
            "(error_type=%s)",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=401,
            detail="Google account access must be reauthorized.",
        ) from error

    verified_email = profile.get("emailAddress")
    if (
        not isinstance(verified_email, str)
        or verified_email.casefold() != request.email.strip().casefold()
    ):
        logger.warning(
            "Google token sync rejected: verified account did not match request"
        )
        raise HTTPException(
            status_code=403,
            detail="Google account does not match the token sync request.",
        )

    refresh_token = request.refresh_token
    if isinstance(refresh_token, str):
        refresh_token = refresh_token if refresh_token.strip() else None

    persistence_started = perf_counter()
    existing_account = (
        db.query(UserAccount)
        .filter(func.lower(UserAccount.email) == verified_email.lower())
        .first()
    )
    previous_access_token = (
        existing_account.access_token if existing_account is not None else None
    )
    statement = postgresql_insert(UserAccount).values(
        id=verified_email.lower(),
        email=verified_email,
        access_token=request.access_token,
        refresh_token=refresh_token,
        token_expiry=request.access_token_expires_at,
        initial_sync_status="not_started",
        initial_sync_eligible=True,
    )
    incoming_token_is_current = or_(
        UserAccount.token_expiry.is_(None),
        and_(
            statement.excluded.token_expiry.is_not(None),
            statement.excluded.token_expiry >= UserAccount.token_expiry,
        ),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[UserAccount.email],
        set_={
            UserAccount.access_token: case(
                (incoming_token_is_current, statement.excluded.access_token),
                else_=UserAccount.access_token,
            ),
            UserAccount.token_expiry: case(
                (
                    incoming_token_is_current,
                    func.coalesce(
                        statement.excluded.token_expiry,
                        UserAccount.token_expiry,
                    ),
                ),
                else_=UserAccount.token_expiry,
            ),
            UserAccount.refresh_token: case(
                (
                    incoming_token_is_current,
                    func.coalesce(
                        func.nullif(statement.excluded.refresh_token, ""),
                        UserAccount.refresh_token,
                    ),
                ),
                else_=UserAccount.refresh_token,
            ),
            UserAccount.updated_at: func.now(),
        },
        where=incoming_token_is_current,
    )
    try:
        db.execute(statement)
        db.commit()
        invalidate_google_profile_verification(previous_access_token)
        invalidate_google_profile_verification(request.access_token)
        logger.info(
            "google_token_sync_timing credential_setup_ms=%.1f "
            "google_profile_ms=%.1f "
            "database_persist_ms=%.1f",
            credential_setup_ms,
            profile_ms,
            (perf_counter() - persistence_started) * 1000,
        )
    except SQLAlchemyError as error:
        logger.error(
            "Google token sync failed during database persistence (error_type=%s)",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to persist Google account tokens.",
        ) from None
    return {"status": "synced"}
