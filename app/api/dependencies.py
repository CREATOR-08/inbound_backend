import logging
from time import perf_counter

from app.db.models import UserAccount
from app.services.gmail import get_gmail_service_from_token
from fastapi import HTTPException
from googleapiclient.errors import HttpError
from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi import Depends, Header
from app.db.database import get_db

logger = logging.getLogger(__name__)


def _get_bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid token header format")
    return token.strip()


def _resolve_user_account(
    access_token: str,
    db: Session,
    create: bool = False,
    timing_context: str | None = None,
) -> tuple[UserAccount, str]:
    try:
        google_profile_started = perf_counter()
        service, active_token = get_gmail_service_from_token(access_token)
        profile = service.users().getProfile(userId="me").execute()
        google_profile_ms = (perf_counter() - google_profile_started) * 1000
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        logger.info("Google rejected Gmail authentication with status %s", status)
        raise HTTPException(
            status_code=status if status in (401, 403) else 502,
            detail="Unable to authenticate this Gmail account.",
        ) from error

    user_email = profile.get("emailAddress")
    if not user_email:
        raise HTTPException(
            status_code=401, detail="Google did not return a Gmail account."
        )

    account_lookup_started = perf_counter()
    user = (
        db.query(UserAccount)
        .filter(func.lower(UserAccount.email) == user_email.lower())
        .first()
    )
    account_lookup_ms = (perf_counter() - account_lookup_started) * 1000
    if timing_context:
        logger.info(
            "%s_auth_timing google_profile_ms=%.1f account_lookup_ms=%.1f",
            timing_context,
            google_profile_ms,
            account_lookup_ms,
        )
    if user is None:
        if not create:
            raise HTTPException(status_code=404, detail="Gmail account not found.")
        user = UserAccount(
            id=user_email.lower(),
            email=user_email,
            access_token=active_token,
        )
        db.add(user)
        db.flush()
    else:
        user.access_token = active_token

    return user, active_token



def get_current_user(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
) -> UserAccount:
    access_token = _get_bearer_token(authorization)

    user, _ = _resolve_user_account(
        access_token=access_token,
        db=db,
        create=False,
        timing_context="assistant",
    )

    return user