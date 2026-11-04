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

from app.api.google_profile_cache import (
    google_profile_verification_cache,
    invalidate_google_profile_verification,
)
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

        previous_access_token = user.access_token
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
            invalidate_google_profile_verification(previous_access_token)
            invalidate_google_profile_verification(access_token)
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
    request_started = perf_counter()
    database_ms = 0.0
    credential_setup_ms = 0.0
    google_profile_ms = 0.0
    google_profile_calls = 0
    profile_cache_hit = False
    persisted_token_state: dict[str, str | None] = {
        "user_id": None,
        "access_token": None,
        "refresh_token": None,
    }

    def log_timing() -> None:
        logger.info(
            "email_auth_timing endpoint=%s "
            "database_lookup_duration_ms=%.1f "
            "google_profile_verification_duration_ms=%.1f "
            "credential_setup_duration_ms=%.1f google_profile_calls=%s "
            "profile_cache=%s total_resolver_duration_ms=%.1f",
            timing_context or "gmail_account",
            database_ms,
            google_profile_ms,
            credential_setup_ms,
            google_profile_calls,
            "hit" if profile_cache_hit else "miss",
            (perf_counter() - request_started) * 1000,
        )

    def persist_refreshed_state(
        refreshed_access: str,
        expiry: datetime | None,
        refreshed_refresh: str | None,
    ) -> None:
        nonlocal database_ms
        user_id = persisted_token_state["user_id"]
        if user_id is None:
            return
        database_started = perf_counter()
        try:
            persisted = _persist_account_token_state(
                user_id,
                refreshed_access,
                expiry,
                refreshed_refresh,
                expected_access_token=persisted_token_state["access_token"],
            )
        finally:
            database_ms += (perf_counter() - database_started) * 1000
        if persisted:
            persisted_token_state["access_token"] = refreshed_access
        if refreshed_refresh:
            persisted_token_state["refresh_token"] = refreshed_refresh

    database_started = perf_counter()
    token_user = (
        db.query(UserAccount)
        .filter(UserAccount.access_token == access_token)
        .first()
    )
    database_ms += (perf_counter() - database_started) * 1000
    token_user_state = (
        {
            "id": token_user.id,
            "email": token_user.email,
            "access_token": token_user.access_token,
            "refresh_token": token_user.refresh_token,
            "token_expiry": token_user.token_expiry,
        }
        if token_user is not None
        else None
    )
    db.rollback()

    try:
        token_expiry = None
        if token_user_state is not None:
            persisted_token_state["user_id"] = token_user_state["id"]
            persisted_token_state["access_token"] = token_user_state[
                "access_token"
            ]

            credential_setup_started = perf_counter()
            try:
                service, active_token, token_expiry = (
                    get_gmail_service_with_token_state(
                        access_token,
                        token_user_state["refresh_token"],
                        token_user_state["token_expiry"],
                        refresh_if_expiry_missing=bool(
                            token_user_state["refresh_token"]
                        ),
                        on_token_refresh=persist_refreshed_state,
                    )
                )
            finally:
                credential_setup_ms += (
                    perf_counter() - credential_setup_started
                ) * 1000
        else:
            credential_setup_started = perf_counter()
            try:
                service, active_token = get_gmail_service_from_token(access_token)
            finally:
                credential_setup_ms += (
                    perf_counter() - credential_setup_started
                ) * 1000

        user_email = None
        if token_user_state is not None and active_token == access_token:
            user_email = google_profile_verification_cache.get(
                access_token,
                token_user_state["email"],
                token_user_state["token_expiry"],
            )
            profile_cache_hit = user_email is not None

        if user_email is None:
            profile_started = perf_counter()
            try:
                google_profile_calls += 1
                profile = service.users().getProfile(userId="me").execute()
            finally:
                google_profile_ms += (perf_counter() - profile_started) * 1000
            user_email = profile.get("emailAddress")
            invalidate_google_profile_verification(access_token)

    except GmailReauthorizationRequired as error:
        invalidate_google_profile_verification(access_token)
        log_timing()
        raise HTTPException(
            status_code=401,
            detail=(
                "Google Gmail access must be reauthorized. Reconnect your "
                "Google account and grant offline Gmail access."
            ),
        ) from error

    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        if status in (401, 403):
            invalidate_google_profile_verification(access_token)
            invalidate_google_profile_verification(
                persisted_token_state["access_token"]
            )
        logger.info("Google rejected Gmail authentication with status %s", status)
        log_timing()

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
        invalidate_google_profile_verification(access_token)
        log_timing()
        logger.info(
            "Google authentication failed: %s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired Google access token.",
        ) from error

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
    database_ms += (perf_counter() - account_lookup_started) * 1000
    if account_by_email is None:
        user = None
    elif (
        token_user_state is None
        or token_user_state["id"] != account_by_email.id
    ):
        previous_access_token = account_by_email.access_token
        previous_refresh_token = account_by_email.refresh_token
        previous_token_expiry = account_by_email.token_expiry
        matched_user_id = account_by_email.id
        persisted_token_state["user_id"] = matched_user_id
        persisted_token_state["access_token"] = previous_access_token
        previous_expiry = (
            previous_token_expiry
            if previous_access_token == access_token
            else None
        )
        db.rollback()
        try:
            credential_setup_started = perf_counter()
            try:
                service, active_token, token_expiry = (
                    get_gmail_service_with_token_state(
                        active_token,
                        previous_refresh_token,
                        previous_expiry,
                        refresh_if_expiry_missing=bool(previous_refresh_token),
                        on_token_refresh=persist_refreshed_state,
                    )
                )
            finally:
                credential_setup_ms += (
                    perf_counter() - credential_setup_started
                ) * 1000
            profile_started = perf_counter()
            try:
                google_profile_calls += 1
                refreshed_profile = (
                    service.users().getProfile(userId="me").execute()
                )
            finally:
                google_profile_ms += (perf_counter() - profile_started) * 1000
        except GmailReauthorizationRequired as error:
            invalidate_google_profile_verification(access_token)
            invalidate_google_profile_verification(active_token)
            log_timing()
            raise HTTPException(
                status_code=401,
                detail=(
                    "Google Gmail access must be reauthorized. Reconnect your "
                    "Google account and grant offline Gmail access."
                ),
            ) from error
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status in (401, 403):
                invalidate_google_profile_verification(access_token)
                invalidate_google_profile_verification(active_token)
            log_timing()
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
        database_started = perf_counter()
        user = (
            db.query(UserAccount)
            .filter(UserAccount.id == matched_user_id)
            .first()
        )
        database_ms += (perf_counter() - database_started) * 1000
        if user is None:
            raise HTTPException(
                status_code=404,
                detail="Gmail account not found.",
            )
    else:
        user = account_by_email

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
        database_started = perf_counter()
        try:
            _persist_account_token_state(
                user.id,
                active_token,
                token_expiry,
                persisted_token_state["refresh_token"],
                expected_access_token=persisted_token_state["access_token"],
            )
            db.expire(user)
            db.refresh(user)
        finally:
            database_ms += (perf_counter() - database_started) * 1000

    if (
        user.email.casefold() == user_email.casefold()
        and user.access_token == active_token
    ):
        google_profile_verification_cache.set(
            active_token,
            user.email,
            user.token_expiry,
        )

    log_timing()

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