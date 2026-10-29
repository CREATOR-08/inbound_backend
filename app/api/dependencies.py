import logging
from dataclasses import dataclass
from time import perf_counter

from app.db.database import SessionLocal
from app.db.models import UserAccount
from app.services.gmail import get_gmail_service_from_token
from fastapi import Header, HTTPException
from googleapiclient.errors import HttpError
from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = [
    "AuthenticatedUser",
    "_get_bearer_token",
    "_resolve_user_account",
    "get_current_user",
]


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str
    access_token: str
    refresh_token: str | None


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
) -> AuthenticatedUser:
    """Resolve the Google identity, then release the DB session before graph work."""
    access_token = _get_bearer_token(authorization)
    try:
        profile_started = perf_counter()
        service, active_token = get_gmail_service_from_token(access_token)
        profile = service.users().getProfile(userId="me").execute()
        profile_ms = (perf_counter() - profile_started) * 1000
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        logger.info("Google rejected assistant Gmail authentication with status %s", status)
        raise HTTPException(
            status_code=status if status in (401, 403) else 502,
            detail="Unable to authenticate this Gmail account.",
        ) from error

    email = profile.get("emailAddress")
    if not email:
        raise HTTPException(
            status_code=401, detail="Google did not return a Gmail account."
        )

    db = SessionLocal()
    try:
        lookup_started = perf_counter()
        user = (
            db.query(UserAccount)
            .filter(func.lower(UserAccount.email) == email.lower())
            .first()
        )
        if user is None:
            raise HTTPException(status_code=404, detail="Gmail account not found.")
        logger.info(
            "assistant_auth_timing google_profile_ms=%.1f account_lookup_ms=%.1f",
            profile_ms,
            (perf_counter() - lookup_started) * 1000,
        )
        return AuthenticatedUser(
            id=user.id,
            email=user.email,
            access_token=active_token,
            refresh_token=user.refresh_token,
        )
    finally:
        db.close()