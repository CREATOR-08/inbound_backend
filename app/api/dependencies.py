import logging
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

import google.auth.exceptions
from fastapi import Header, HTTPException
from googleapiclient.errors import HttpError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import UserAccount
from app.services.gmail import (
    GmailReauthorizationRequired,
    get_gmail_service_from_token,
    get_gmail_service_with_token_state,
)

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


def _get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header.",
        )

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header.",
        )

    return token.strip()


def _update_account_token_state(
    user: UserAccount,
    access_token: str,
    token_expiry: datetime | None,
    refresh_token: str | None,
) -> None:
    user.access_token = access_token
    user.token_expiry = token_expiry
    if refresh_token:
        user.refresh_token = refresh_token


def _resolve_user_account(
    access_token: str,
    db: Session,
    create: bool = False,
    timing_context: str | None = None,
) -> tuple[UserAccount, str]:
    try:
        google_profile_started = perf_counter()
        user = (
            db.query(UserAccount)
            .filter(UserAccount.access_token == access_token)
            .first()
        )
        token_expiry = None
        if user is not None:
            service, active_token, token_expiry = get_gmail_service_with_token_state(
                access_token,
                user.refresh_token,
                user.token_expiry,
                refresh_if_expiry_missing=bool(user.refresh_token),
                on_token_refresh=lambda refreshed_access, expiry, refreshed_refresh: (
                    _update_account_token_state(
                        user,
                        refreshed_access,
                        expiry,
                        refreshed_refresh,
                    )
                ),
            )
        else:
            service, active_token = get_gmail_service_from_token(access_token)
        profile = service.users().getProfile(userId="me").execute()
        google_profile_ms = (perf_counter() - google_profile_started) * 1000

    except GmailReauthorizationRequired as error:
        raise HTTPException(
            status_code=401,
            detail=(
                "Google Gmail access must be reauthorized. Reconnect your "
                "Google account and grant offline Gmail access."
            ),
        ) from error

    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        logger.info("Google rejected Gmail authentication with status %s", status)

        if status in (401, 403):
            raise HTTPException(
                status_code=401 if status == 401 else 403,
                detail=(
                    "Google Gmail access must be reauthorized. Reconnect your "
                    "Google account and grant offline Gmail access."
                    if status == 401
                    else "Google Gmail access is denied."
                ),
            ) from error

        raise HTTPException(
            status_code=502,
            detail="Unable to authenticate this Gmail account.",
        ) from error

    except (
        google.auth.exceptions.RefreshError,
        google.auth.exceptions.GoogleAuthError,
    ) as error:
        logger.info(
            "Google authentication failed: %s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired Google access token.",
        ) from error

    user_email = profile.get("emailAddress")
    if not user_email:
        raise HTTPException(
            status_code=401,
            detail="Google did not return a Gmail account.",
        )

    account_lookup_started = perf_counter()
    account_by_email = (
        db.query(UserAccount)
        .filter(func.lower(UserAccount.email) == user_email.lower())
        .first()
    )
    if account_by_email is None:
        user = None
    elif user is None or user.id != account_by_email.id:
        user = account_by_email
        previous_access_token = user.access_token
        previous_expiry = (
            user.token_expiry if previous_access_token == access_token else None
        )
        try:
            service, active_token, token_expiry = get_gmail_service_with_token_state(
                active_token,
                user.refresh_token,
                previous_expiry,
                refresh_if_expiry_missing=bool(user.refresh_token),
                on_token_refresh=lambda refreshed_access, expiry, refreshed_refresh: (
                    _update_account_token_state(
                        user,
                        refreshed_access,
                        expiry,
                        refreshed_refresh,
                    )
                ),
            )
            refreshed_profile = service.users().getProfile(userId="me").execute()
        except GmailReauthorizationRequired as error:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Google Gmail access must be reauthorized. Reconnect your "
                    "Google account and grant offline Gmail access."
                ),
            ) from error
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status == 401:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Google Gmail access must be reauthorized. Reconnect "
                        "your Google account and grant offline Gmail access."
                    ),
                ) from error
            raise HTTPException(
                status_code=502,
                detail="Unable to authenticate this Gmail account.",
            ) from error
        if (
            refreshed_profile.get("emailAddress", "").lower()
            != user_email.lower()
        ):
            raise HTTPException(
                status_code=401,
                detail="Google account credentials do not match the authenticated account.",
            )
    else:
        user = account_by_email

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
            raise HTTPException(
                status_code=404,
                detail="Gmail account not found.",
            )

        user = UserAccount(
            id=user_email.lower(),
            email=user_email,
            access_token=active_token,
            token_expiry=token_expiry,
        )
        db.add(user)
        db.flush()
    else:
        user.access_token = active_token
        user.token_expiry = token_expiry

    return user, active_token


def get_current_user(
    authorization: str = Header(...),
) -> AuthenticatedUser:
    """Resolve the Google identity, then release the DB session before graph work."""
    access_token = _get_bearer_token(authorization)
    credential_db = SessionLocal()
    try:
        stored_user = (
            credential_db.query(UserAccount)
            .filter(UserAccount.access_token == access_token)
            .first()
        )
        stored_credentials = (
            (
                stored_user.refresh_token,
                stored_user.token_expiry,
            )
            if stored_user is not None
            else None
        )
    finally:
        credential_db.close()

    refreshed_token_state = {}

    def store_refreshed_state(
        refreshed_access_token: str,
        expiry: datetime | None,
        refresh_token: str | None,
    ) -> None:
        refreshed_token_state["access_token"] = refreshed_access_token
        refreshed_token_state["expiry"] = expiry
        if refresh_token:
            refreshed_token_state["refresh_token"] = refresh_token

    try:
        profile_started = perf_counter()
        if stored_credentials is None:
            service, active_token = get_gmail_service_from_token(access_token)
            token_expiry = None
        else:
            refresh_token, stored_expiry = stored_credentials
            service, active_token, token_expiry = (
                get_gmail_service_with_token_state(
                    access_token,
                    refresh_token,
                    stored_expiry,
                    refresh_if_expiry_missing=bool(refresh_token),
                )
            )
        profile = service.users().getProfile(userId="me").execute()
        profile_ms = (perf_counter() - profile_started) * 1000
    except GmailReauthorizationRequired as error:
        raise HTTPException(
            status_code=401,
            detail=(
                "Google Gmail access must be reauthorized. Reconnect your "
                "Google account and grant offline Gmail access."
            ),
        ) from error
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        logger.info("Google rejected assistant Gmail authentication with status %s", status)
        raise HTTPException(
            status_code=status if status in (401, 403) else 502,
            detail=(
                "Google Gmail access must be reauthorized. Reconnect your "
                "Google account and grant offline Gmail access."
                if status == 401
                else "Unable to authenticate this Gmail account."
            ),
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
        if stored_credentials is None and user.refresh_token:
            try:
                service, active_token, token_expiry = (
                    get_gmail_service_with_token_state(
                        active_token,
                        user.refresh_token,
                        user.token_expiry
                        if user.access_token == access_token
                        else None,
                        refresh_if_expiry_missing=True,
                        on_token_refresh=store_refreshed_state,
                    )
                )
                refreshed_profile = (
                    service.users().getProfile(userId="me").execute()
                )
            except GmailReauthorizationRequired as error:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Google Gmail access must be reauthorized. Reconnect "
                        "your Google account and grant offline Gmail access."
                    ),
                ) from error
            except HttpError as error:
                status = getattr(getattr(error, "resp", None), "status", None)
                raise HTTPException(
                    status_code=401 if status in (401, 403) else 502,
                    detail=(
                        "Google Gmail access must be reauthorized. Reconnect "
                        "your Google account and grant offline Gmail access."
                        if status == 401
                        else "Unable to authenticate this Gmail account."
                    ),
                ) from error
            if refreshed_profile.get("emailAddress", "").lower() != email.lower():
                raise HTTPException(
                    status_code=401,
                    detail="Google account credentials do not match the authenticated account.",
                )
        user.access_token = active_token
        user.token_expiry = refreshed_token_state.get("expiry", token_expiry)
        if refreshed_token_state.get("refresh_token"):
            user.refresh_token = refreshed_token_state["refresh_token"]
        db.commit()
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