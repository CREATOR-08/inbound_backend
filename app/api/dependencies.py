import logging
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

import google.auth.exceptions
from fastapi import Header, HTTPException
from googleapiclient.errors import HttpError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
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

_ANY_ACCESS_TOKEN = object()


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str


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


def _persist_account_token_state(
    user_id: str,
    access_token: str,
    token_expiry: datetime | None,
    refresh_token: str | None = None,
    *,
    expected_access_token: str | None | object = _ANY_ACCESS_TOKEN,
) -> bool:
    db = SessionLocal()
    try:
        user = (
            db.query(UserAccount)
            .filter(UserAccount.id == user_id)
            .with_for_update()
            .first()
        )
        if user is None:
            raise RuntimeError("Gmail account disappeared while saving credentials.")
        if (
            expected_access_token is not _ANY_ACCESS_TOKEN
            and user.access_token != expected_access_token
        ):
            return False

        changed = user.access_token != access_token
        user.access_token = access_token
        if token_expiry is not None and user.token_expiry != token_expiry:
            user.token_expiry = token_expiry
            changed = True
        if refresh_token and refresh_token.strip() and user.refresh_token != refresh_token:
            user.refresh_token = refresh_token
            changed = True
        if changed:
            db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _resolve_user_account(
    access_token: str,
    db: Session,
    create: bool = False,
    timing_context: str | None = None,
) -> tuple[UserAccount, str]:
    persisted_token_state: dict[str, str | None] = {
        "user_id": None,
        "access_token": None,
        "refresh_token": None,
    }

    def persist_refreshed_state(
        refreshed_access: str,
        expiry: datetime | None,
        refreshed_refresh: str | None,
    ) -> None:
        user_id = persisted_token_state["user_id"]
        if user_id is None:
            return
        persisted = _persist_account_token_state(
            user_id,
            refreshed_access,
            expiry,
            refreshed_refresh,
            expected_access_token=persisted_token_state["access_token"],
        )
        if persisted:
            persisted_token_state["access_token"] = refreshed_access
        if refreshed_refresh:
            persisted_token_state["refresh_token"] = refreshed_refresh

    try:
        google_profile_started = perf_counter()
        user = (
            db.query(UserAccount)
            .filter(UserAccount.access_token == access_token)
            .first()
        )
        token_expiry = None
        if user is not None:
            persisted_token_state["user_id"] = user.id
            persisted_token_state["access_token"] = user.access_token

            service, active_token, token_expiry = get_gmail_service_with_token_state(
                access_token,
                user.refresh_token,
                user.token_expiry,
                refresh_if_expiry_missing=bool(user.refresh_token),
                on_token_refresh=persist_refreshed_state,
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
        persisted_token_state["user_id"] = user.id
        persisted_token_state["access_token"] = previous_access_token
        previous_expiry = (
            user.token_expiry if previous_access_token == access_token else None
        )
        try:
            service, active_token, token_expiry = get_gmail_service_with_token_state(
                active_token,
                user.refresh_token,
                previous_expiry,
                refresh_if_expiry_missing=bool(user.refresh_token),
                on_token_refresh=persist_refreshed_state,
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

    created_account = False
    if user is None:
        if not create:
            raise HTTPException(
                status_code=404,
                detail="Gmail account not found.",
            )

        account_values = {
            "id": user_email.lower(),
            "email": user_email,
            "access_token": active_token,
            "token_expiry": token_expiry,
            "initial_sync_status": "not_started",
            "initial_sync_eligible": True,
        }
        if db.get_bind().dialect.name == "postgresql":
            inserted_id = db.execute(
                postgresql_insert(UserAccount)
                .values(**account_values)
                .on_conflict_do_nothing()
                .returning(UserAccount.id)
            ).scalar_one_or_none()
            user = (
                db.query(UserAccount)
                .filter(func.lower(UserAccount.email) == user_email.lower())
                .first()
            )
            if user is None:
                raise RuntimeError("Unable to resolve the created Gmail account.")
            created_account = inserted_id is not None
        else:
            user = UserAccount(**account_values)
            db.add(user)
            db.flush()
            created_account = True

    if not created_account:
        if persisted_token_state["user_id"] is None:
            persisted_token_state["user_id"] = user.id
            persisted_token_state["access_token"] = user.access_token
        _persist_account_token_state(
            user.id,
            active_token,
            token_expiry,
            persisted_token_state["refresh_token"],
            expected_access_token=persisted_token_state["access_token"],
        )
        db.expire(user)
        db.refresh(user)

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
                stored_user.id,
                stored_user.access_token,
                stored_user.refresh_token,
                stored_user.token_expiry,
            )
            if stored_user is not None
            else None
        )
    finally:
        credential_db.close()

    refreshed_token_state = {}
    persisted_token_state = {
        "user_id": stored_credentials[0] if stored_credentials else None,
        "access_token": stored_credentials[1] if stored_credentials else None,
    }

    def store_refreshed_state(
        refreshed_access_token: str,
        expiry: datetime | None,
        refresh_token: str | None,
    ) -> None:
        if persisted_token_state["user_id"] is not None:
            persisted = _persist_account_token_state(
                persisted_token_state["user_id"],
                refreshed_access_token,
                expiry,
                refresh_token,
                expected_access_token=persisted_token_state["access_token"],
            )
            if persisted:
                persisted_token_state["access_token"] = refreshed_access_token
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
            _, _, refresh_token, stored_expiry = stored_credentials
            service, active_token, token_expiry = (
                get_gmail_service_with_token_state(
                    access_token,
                    refresh_token,
                    stored_expiry,
                    refresh_if_expiry_missing=bool(refresh_token),
                    on_token_refresh=store_refreshed_state,
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
        if stored_credentials is None:
            persisted_token_state["user_id"] = user.id
            persisted_token_state["access_token"] = user.access_token
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
        _persist_account_token_state(
            user.id,
            active_token,
            refreshed_token_state.get("expiry", token_expiry),
            refreshed_token_state.get("refresh_token"),
            expected_access_token=persisted_token_state["access_token"],
        )
        db.expire(user)
        db.refresh(user)
        logger.info(
            "assistant_auth_timing google_profile_ms=%.1f account_lookup_ms=%.1f",
            profile_ms,
            (perf_counter() - lookup_started) * 1000,
        )
        return AuthenticatedUser(
            id=user.id,
            email=user.email,
        )
    finally:
        db.close()