import base64
import binascii
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import perf_counter
from typing import Callable, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
)
from fastapi.responses import StreamingResponse
from googleapiclient.errors import HttpError
from sqlalchemy import and_, func, or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, joinedload, load_only

from app.agent.graph import triage_graph
from app.agent.nodes import stream_draft_reply
from app.api.dependencies import (
    _get_bearer_token,
    _persist_account_token_state,
    _resolve_user_account,
)
from app.api.routes.common import (
    VALID_CATEGORIES,
    VALID_PRIORITIES,
    _build_action_state,
    _get_email_action_state,
)
from app.api.schemas.schemas import (
    EmailActionRequest,
    EmailReadUpdateRequest,
    EmailSendRequest,
    InitialSyncResponse,
    InitialSyncStatusResponse,
    PubSubPushRequest,
)
from app.db.database import SessionLocal, get_db
from app.db.models import Email, EmailAgentStateModel, UserAccount
from app.services.gmail import (
    GmailHistoryCursorExpired,
    GmailReauthorizationRequired,
    TokenRefreshCallback,
    fetch_new_message_ids_from_history,
    fetch_single_email,
    get_gmail_service_with_token_state,
    list_message_ids,
    send_email_reply,
    start_gmail_watch,
    update_gmail_message_read_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()
action_router = APIRouter()
INITIAL_SYNC_RECOVERY_TIMEOUT_SECONDS = 300
DEFAULT_INITIAL_GMAIL_FETCH_CONCURRENCY = 5
INITIAL_GMAIL_MESSAGE_FETCH_RETRIES = 2


def _initial_gmail_fetch_concurrency() -> int:
    configured_limit = os.getenv(
        "INITIAL_GMAIL_FETCH_CONCURRENCY",
        str(DEFAULT_INITIAL_GMAIL_FETCH_CONCURRENCY),
    )
    try:
        concurrency_limit = int(configured_limit)
    except ValueError as exc:
        raise RuntimeError(
            "INITIAL_GMAIL_FETCH_CONCURRENCY must be a positive integer."
        ) from exc
    if concurrency_limit < 1:
        raise RuntimeError(
            "INITIAL_GMAIL_FETCH_CONCURRENCY must be a positive integer."
        )
    return concurrency_limit


def _fetch_initial_gmail_messages(
    message_ids: list[str],
    fetch_message: Callable[[str], dict],
    concurrency_limit: int,
) -> list[dict]:
    unique_message_ids = list(dict.fromkeys(message_ids))
    worker_count = min(concurrency_limit, len(unique_message_ids))
    started_at = perf_counter()
    fetched_by_index: dict[int, dict] = {}
    failure_count = 0
    first_error: Exception | None = None

    try:
        if worker_count:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(fetch_message, message_id): index
                    for index, message_id in enumerate(unique_message_ids)
                }
                for future in as_completed(futures):
                    try:
                        fetched_by_index[futures[future]] = future.result()
                    except Exception as exc:
                        failure_count += 1
                        if first_error is None:
                            first_error = exc
                        logger.warning(
                            "Initial Gmail message fetch failed (error_type=%s)",
                            type(exc).__name__,
                        )
    finally:
        logger.info(
            "Initial Gmail message fetching finished message_count=%s "
            "concurrency_limit=%s active_workers=%s duration_ms=%.1f "
            "failure_count=%s",
            len(unique_message_ids),
            concurrency_limit,
            worker_count,
            (perf_counter() - started_at) * 1000,
            failure_count,
        )

    if first_error is not None:
        raise first_error
    return [fetched_by_index[index] for index in range(len(unique_message_ids))]


def _watch_is_active(expiration: str | None) -> bool:
    if not expiration:
        return False
    try:
        return int(expiration) > int(datetime.now(timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        return False


def _ensure_gmail_watch(
    user: UserAccount,
    access_token: str,
    on_token_refresh: TokenRefreshCallback | None = None,
) -> dict[str, str]:
    if user.last_history_id and _watch_is_active(user.watch_expiration):
        return {
            "historyId": user.last_history_id,
            "expiration": user.watch_expiration or "",
        }

    result = start_gmail_watch(
        access_token,
        user.refresh_token,
        token_expiry=user.token_expiry,
        on_token_refresh=on_token_refresh,
    )
    history_id = result.get("historyId")
    expiration = result.get("expiration")
    if not history_id or not expiration:
        raise RuntimeError("Gmail did not return a valid watch cursor.")

    user.last_history_id = user.last_history_id or str(history_id)
    user.watch_expiration = str(expiration)
    return {
        "historyId": user.last_history_id,
        "expiration": str(expiration),
    }


def _register_user_gmail_watch(
    user: UserAccount,
    access_token: str,
) -> dict[str, str]:
    user_id = user.id
    token_state = {"access_token": access_token}
    watch_user = UserAccount(
        id=user_id,
        refresh_token=user.refresh_token,
        token_expiry=user.token_expiry,
        last_history_id=user.last_history_id,
        watch_expiration=user.watch_expiration,
    )

    def persist_refreshed_token(
        refreshed_access_token: str,
        expiry: datetime | None,
        refreshed_refresh_token: str | None,
    ) -> None:
        _persist_gmail_token_state(
            user_id,
            refreshed_access_token,
            expiry,
            refreshed_refresh_token,
            expected_access_token=token_state["access_token"],
        )
        token_state["access_token"] = refreshed_access_token

    watch_res = _ensure_gmail_watch(
        watch_user,
        access_token,
        persist_refreshed_token,
    )
    storage_db = SessionLocal()
    try:
        stored_user = (
            storage_db.query(UserAccount)
            .filter(UserAccount.id == user_id)
            .with_for_update()
            .one()
        )
        if not stored_user.last_history_id:
            stored_user.last_history_id = watch_res["historyId"]
        stored_user.watch_expiration = watch_res["expiration"]
        storage_db.commit()
        return {
            "historyId": stored_user.last_history_id,
            "expiration": stored_user.watch_expiration,
        }
    except Exception:
        storage_db.rollback()
        raise
    finally:
        storage_db.close()


def _latest_history_id(*history_ids: str | None) -> str | None:
    valid_ids = [history_id for history_id in history_ids if history_id]
    return max(valid_ids, key=int) if valid_ids else None


def _persist_gmail_token_state(
    user_id: str,
    access_token: str,
    token_expiry: datetime | None,
    refresh_token: str | None = None,
    *,
    expected_access_token: str | None = None,
) -> bool:
    if expected_access_token is None:
        return _persist_account_token_state(
            user_id,
            access_token,
            token_expiry,
            refresh_token,
        )
    return _persist_account_token_state(
        user_id,
        access_token,
        token_expiry,
        refresh_token,
        expected_access_token=expected_access_token,
    )


def _store_email_if_new(db: Session, user: UserAccount, email_data: dict) -> bool:
    email_values = {
        "id": email_data["id"],
        "thread_id": email_data["thread_id"],
        "user_id": user.id,
        "sender": email_data["sender"],
        "subject": email_data["subject"],
        "preview": email_data["preview"],
        "body": email_data["body"],
        "body_html": email_data["body_html"],
        "has_html": email_data["has_html"],
        "created_at": email_data["received_at"],
        "is_read": bool(email_data.get("is_read", False)),
    }
    if db.get_bind().dialect.name == "postgresql":
        inserted = db.execute(
            postgresql_insert(Email)
            .values(**email_values)
            .on_conflict_do_nothing(index_elements=[Email.id])
        )
        if not inserted.rowcount:
            existing = db.query(Email).filter(Email.id == email_data["id"]).first()
            if existing and existing.user_id != user.id:
                logger.error(
                    "Gmail message ID collision across accounts; refusing to reassign it"
                )
            return False
        db.execute(
            postgresql_insert(EmailAgentStateModel)
            .values(
                email_id=email_data["id"],
                category=None,
                priority=None,
                is_archived=False,
            )
            .on_conflict_do_nothing(index_elements=[EmailAgentStateModel.email_id])
        )

        return True

    existing = db.query(Email).filter(Email.id == email_data["id"]).first()
    if existing:
        if existing.user_id != user.id:
            logger.error(
                "Gmail message ID collision across accounts; refusing to reassign it"
            )
        return False

    db.add(Email(**email_values))
    db.add(
        EmailAgentStateModel(
            email_id=email_data["id"],
            category=None,
            priority=None,
            is_archived=False,
        )
    )
    return True


def tag_recent_untagged_emails(user_id: str, limit: int = 10) -> None:
    """Tag recent emails without keeping a session or lock during model inference."""
    logger.info("Starting background tagging for user_id=%s limit=%s", user_id, limit)
    attempted_ids: set[str] = set()
    processed = 0
    updated = 0
    while processed < limit:
        db: Session = SessionLocal()
        try:
            needs_tagging = or_(
                Email.category.is_(None),
                Email.priority.is_(None),
                Email.category.notin_(VALID_CATEGORIES),
                Email.priority.notin_(VALID_PRIORITIES),
                EmailAgentStateModel.category.is_(None),
                EmailAgentStateModel.priority.is_(None),
                EmailAgentStateModel.category.notin_(VALID_CATEGORIES),
                EmailAgentStateModel.priority.notin_(VALID_PRIORITIES),
            )
            email = (
                db.query(Email)
                .outerjoin(Email.state)
                .filter(
                    Email.user_id == user_id,
                    needs_tagging,
                    ~Email.id.in_(attempted_ids) if attempted_ids else True,
                )
                .order_by(Email.created_at.desc())
                .first()
            )
            if email is None:
                email_id = None
                action_state = None
            else:
                email_id = email.id
                action_state = _build_action_state(email)
                attempted_ids.add(email_id)
        except Exception:
            logger.exception("Failed selecting untagged emails for user_id=%s", user_id)
            return
        finally:
            db.close()

        if email_id is None or action_state is None:
            break
        processed += 1
        logger.info(
            "Tagging email_id=%s for user_id=%s (%s/%s)",
            email_id,
            user_id,
            processed,
            limit,
        )
        try:
            result = triage_graph.invoke(action_state)
            category = result.get("category")
            priority = result.get("priority")
            if category not in VALID_CATEGORIES or priority not in VALID_PRIORITIES:
                raise ValueError("Triage returned an invalid category or priority")
        except Exception:
            logger.exception(
                "Failed to tag email_id=%s for user_id=%s", email_id, user_id
            )
            continue

        db = SessionLocal()
        try:
            email = (
                db.query(Email)
                .filter(Email.id == email_id, Email.user_id == user_id)
                .first()
            )
            if email is None:
                continue
            state = email.state
            if state is None:
                state = EmailAgentStateModel(email_id=email_id)
                db.add(state)
            is_archived = bool(result.get("is_archived", False))
            email.category = category
            email.priority = priority
            email.is_archived = is_archived
            state.category = category
            state.priority = priority
            state.is_archived = is_archived
            db.commit()
            updated += 1
            logger.info(
                "Tagging succeeded for email_id=%s: category=%s priority=%s is_archived=%s",
                email_id,
                category,
                priority,
                is_archived,
            )
        except Exception:
            db.rollback()
            logger.exception("Failed to persist triage for email_id=%s", email_id)
        finally:
            db.close()
    logger.info(
        "Background tagging complete for user_id=%s processed=%s updated=%s",
        user_id,
        processed,
        updated,
    )


@router.post("/emails/watch")
def register_watch(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Registers Gmail to publish inbox events to Pub/Sub and saves initial historyId."""
    token = _get_bearer_token(authorization)

    try:
        user, active_token = _resolve_user_account(token, db, create=True)
        watch_user = UserAccount(
            id=user.id,
            refresh_token=user.refresh_token,
            token_expiry=user.token_expiry,
            last_history_id=user.last_history_id,
            watch_expiration=user.watch_expiration,
        )
        db.commit()
        db.close()
        watch_res = _register_user_gmail_watch(watch_user, active_token)
        return {"status": "subscribed", "data": watch_res}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Failed to register Gmail watch")
        raise HTTPException(
            status_code=502, detail="Failed to activate the Gmail watch."
        ) from e


def _mark_initial_sync_failed(user_id: str) -> None:
    db: Session = SessionLocal()
    try:
        user = db.query(UserAccount).filter(UserAccount.id == user_id).first()
        if user is not None and user.initial_sync_completed_at is None:
            user.initial_sync_status = "failed"
            user.initial_sync_eligible = False
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _claim_stale_initial_sync(db: Session, user_id: str) -> int:
    recovery_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=INITIAL_SYNC_RECOVERY_TIMEOUT_SECONDS
    )
    stale_claim = or_(
        UserAccount.updated_at < recovery_cutoff,
        and_(
            UserAccount.updated_at.is_(None),
            UserAccount.created_at < recovery_cutoff,
        ),
    )
    return (
        db.query(UserAccount)
        .filter(
            UserAccount.id == user_id,
            UserAccount.initial_sync_eligible.is_(True),
            UserAccount.initial_sync_status == "pending",
            stale_claim,
        )
        .update(
            {UserAccount.updated_at: func.now()},
            synchronize_session=False,
        )
    )


def run_initial_gmail_sync(user_id: str) -> None:
    """Run the initial Gmail import using worker-owned database sessions."""
    try:
        db: Session = SessionLocal()
        try:
            claimed = (
                db.query(UserAccount)
                .filter(
                    UserAccount.id == user_id,
                    UserAccount.initial_sync_eligible.is_(True),
                    UserAccount.initial_sync_status == "pending",
                )
                .update(
                    {
                        UserAccount.initial_sync_status: "running",
                        UserAccount.updated_at: func.now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if not claimed:
                return

            user = db.query(UserAccount).filter(UserAccount.id == user_id).first()
            if user is None:
                raise RuntimeError("Gmail account disappeared before synchronization.")
            if not user.initial_sync_eligible:
                return
            if user.initial_sync_completed_at:
                user.initial_sync_status = "completed"
                user.initial_sync_eligible = False
                db.commit()
                return
            if user.initial_sync_status != "running":
                return

            active_token = user.access_token
            refresh_token = user.refresh_token
            token_expiry = user.token_expiry
            if not isinstance(active_token, str) or not active_token:
                raise RuntimeError("Gmail account has no access token.")
            if refresh_token is not None and not isinstance(refresh_token, str):
                raise RuntimeError("Gmail account has an invalid refresh token.")
            if token_expiry is not None and not isinstance(token_expiry, datetime):
                raise RuntimeError("Gmail account has an invalid token expiry.")
            watch_user = UserAccount(
                id=user.id,
                refresh_token=refresh_token,
                token_expiry=token_expiry,
                last_history_id=user.last_history_id,
                watch_expiration=user.watch_expiration,
            )
            db.commit()
        finally:
            db.close()

        token_state = {"access_token": active_token, "expiry": token_expiry}

        def persist_refreshed_token(
            refreshed_access_token: str,
            expiry: datetime | None,
            refreshed_refresh_token: str | None,
        ) -> None:
            nonlocal refresh_token
            _persist_gmail_token_state(
                user_id,
                refreshed_access_token,
                expiry,
                refreshed_refresh_token,
                expected_access_token=token_state["access_token"],
            )
            token_state["access_token"] = refreshed_access_token
            token_state["expiry"] = expiry
            if refreshed_refresh_token and refreshed_refresh_token.strip():
                refresh_token = refreshed_refresh_token

        _ensure_gmail_watch(
            watch_user,
            active_token,
            persist_refreshed_token,
        )
        active_token = token_state["access_token"]
        start_history_id = watch_user.last_history_id
        if not start_history_id:
            raise RuntimeError("Gmail watch did not save a history cursor.")

        db = SessionLocal()
        try:
            stored_user = (
                db.query(UserAccount)
                .filter(UserAccount.id == user_id)
                .with_for_update()
                .one()
            )
            stored_user.watch_expiration = watch_user.watch_expiration
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        listed_ids, active_token = list_message_ids(
            access_token=active_token,
            query="in:inbox newer_than:3d",
            refresh_token=refresh_token,
            token_expiry=token_state["expiry"],
            on_token_refresh=persist_refreshed_token,
        )
        token_state["access_token"] = active_token
        changed_ids, latest_history_id, active_token = (
            fetch_new_message_ids_from_history(
                access_token=active_token,
                start_history_id=start_history_id,
                refresh_token=refresh_token,
                raise_on_expired=True,
                token_expiry=token_state["expiry"],
                on_token_refresh=persist_refreshed_token,
            )
        )
        token_state["access_token"] = active_token

        message_ids = list(dict.fromkeys([*listed_ids, *changed_ids]))
        fetch_token_lock = Lock()

        def persist_parallel_refreshed_token(
            refreshed_access_token: str,
            expiry: datetime | None,
            refreshed_refresh_token: str | None,
        ) -> None:
            nonlocal refresh_token
            with fetch_token_lock:
                persisted = _persist_gmail_token_state(
                    user_id,
                    refreshed_access_token,
                    expiry,
                    refreshed_refresh_token,
                    expected_access_token=token_state["access_token"],
                )
                if not persisted:
                    return
                token_state["access_token"] = refreshed_access_token
                token_state["expiry"] = expiry
                if (
                    refreshed_refresh_token
                    and refreshed_refresh_token.strip()
                ):
                    refresh_token = refreshed_refresh_token

        def fetch_initial_message(message_id: str) -> dict:
            with fetch_token_lock:
                message_access_token = token_state["access_token"]
                message_refresh_token = refresh_token
                message_token_expiry = token_state["expiry"]
            return fetch_single_email(
                access_token=message_access_token,
                message_id=message_id,
                refresh_token=message_refresh_token,
                token_expiry=message_token_expiry,
                on_token_refresh=persist_parallel_refreshed_token,
                num_retries=INITIAL_GMAIL_MESSAGE_FETCH_RETRIES,
            )

        fetched_emails = _fetch_initial_gmail_messages(
            message_ids,
            fetch_initial_message,
            _initial_gmail_fetch_concurrency(),
        )

        db = SessionLocal()
        try:
            user = (
                db.query(UserAccount)
                .filter(UserAccount.id == user_id)
                .with_for_update()
                .one()
            )
            if user.initial_sync_completed_at:
                user.initial_sync_status = "completed"
                db.commit()
                return
            imported_count = sum(
                _store_email_if_new(db, user, email_data)
                for email_data in fetched_emails
            )
            user.last_history_id = (
                _latest_history_id(user.last_history_id, latest_history_id)
                or start_history_id
            )
            user.initial_sync_completed_at = datetime.now(timezone.utc)
            user.initial_sync_status = "completed"
            user.initial_sync_eligible = False
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        logger.info(
            "Initial Gmail synchronization completed for user_id=%s imported=%s skipped=%s",
            user_id,
            imported_count,
            len(message_ids) - imported_count,
        )
        try:
            tag_recent_untagged_emails(user_id)
        except Exception:
            logger.exception(
                "Initial Gmail synchronization tagging failed for user_id=%s",
                user_id,
            )
    except Exception:
        logger.exception("Initial Gmail synchronization failed for user_id=%s", user_id)
        try:
            _mark_initial_sync_failed(user_id)
        except Exception:
            logger.exception(
                "Could not record initial Gmail synchronization failure for user_id=%s",
                user_id,
            )
        raise


@router.post(
    "/emails/sync-initial",
    response_model=InitialSyncResponse,
    responses={202: {"model": InitialSyncResponse}},
)
def sync_initial_emails(
    background_tasks: BackgroundTasks,
    response: Response,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Queue first-time imports or incrementally catch up an existing account."""
    try:
        user, _ = _resolve_user_account(
            _get_bearer_token(authorization),
            db,
            create=True,
            timing_context="sync_initial_emails",
        )
        user_id = user.id
        db.commit()

        current_status = getattr(user, "initial_sync_status", None)
        initial_sync_eligible = bool(
            getattr(user, "initial_sync_eligible", False)
        )
        if initial_sync_eligible and current_status == "not_started":
            claimed = (
                db.query(UserAccount)
                .filter(
                    UserAccount.id == user_id,
                    UserAccount.initial_sync_eligible.is_(True),
                    UserAccount.initial_sync_status == "not_started",
                )
                .update(
                    {
                        UserAccount.initial_sync_status: "pending",
                        UserAccount.updated_at: func.now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if claimed:
                history_id = user.last_history_id or ""
                user.initial_sync_status = "pending"
                db.close()
                background_tasks.add_task(run_initial_gmail_sync, user_id)
                response.status_code = 202
                return InitialSyncResponse(
                    status="pending",
                    imported_count=0,
                    skipped_count=0,
                    history_id=history_id,
                    account_status="new",
                    sync_mode="initial",
                )
            db.refresh(user)
            current_status = getattr(user, "initial_sync_status", None)
            initial_sync_eligible = bool(
                getattr(user, "initial_sync_eligible", False)
            )

        if initial_sync_eligible and current_status == "pending":
            reclaimed = _claim_stale_initial_sync(db, user_id)
            db.commit()
            if reclaimed:
                history_id = user.last_history_id or ""
                db.close()
                background_tasks.add_task(run_initial_gmail_sync, user_id)
                response.status_code = 202
                return InitialSyncResponse(
                    status="pending",
                    imported_count=0,
                    skipped_count=0,
                    history_id=history_id,
                    account_status="new",
                    sync_mode="initial",
                )
            db.refresh(user)
            current_status = getattr(user, "initial_sync_status", None)
            initial_sync_eligible = bool(
                getattr(user, "initial_sync_eligible", False)
            )

        if current_status in ("pending", "running"):
            if initial_sync_eligible:
                history_id = user.last_history_id or ""
                db.close()
                response.status_code = 202
                return InitialSyncResponse(
                    status=current_status,
                    imported_count=0,
                    skipped_count=0,
                    history_id=history_id,
                    account_status="new",
                    sync_mode="initial",
                )

        history_id = user.last_history_id or ""
        initial_status = current_status or "not_started"
        initial_response_status = (
            "already_completed" if initial_status == "completed" else initial_status
        )
        watch_user = UserAccount(
            id=user.id,
            email=user.email,
            refresh_token=user.refresh_token,
            token_expiry=user.token_expiry,
            last_history_id=user.last_history_id,
            watch_expiration=user.watch_expiration,
        )
        active_token = user.access_token
        email_address = user.email
        stored_email_count = (
            db.query(func.count(Email.id))
            .filter(Email.user_id == user_id)
            .scalar()
            or 0
        )
        db.close()

        execution_id = str(uuid.uuid4())
        if not _claim_incremental_sync(user_id, execution_id):
            raise HTTPException(
                status_code=404,
                detail="Gmail account not found.",
            )
        try:
            watch_res = _register_user_gmail_watch(watch_user, active_token)
            process_realtime_email_update(
                email_address,
                watch_res["historyId"],
                True,
                execution_id,
            )
        except Exception as exc:
            try:
                _set_incremental_sync_status(
                    user_id,
                    "failed",
                    execution_id,
                )
            except Exception:
                logger.exception(
                    "Could not record incremental Gmail synchronization failure "
                    "for user_id=%s",
                    user_id,
                )
            logger.exception(
                "Existing Gmail account incremental catch-up failed for user_id=%s",
                user_id,
            )
            raise HTTPException(
                status_code=503,
                detail="Existing Gmail account synchronization failed.",
            ) from exc

        status_db = SessionLocal()
        try:
            stored_email_count = (
                status_db.query(func.count(Email.id))
                .filter(Email.user_id == user_id)
                .scalar()
                or 0
            )
            incremental_status = (
                status_db.query(UserAccount.incremental_sync_status)
                .filter(UserAccount.id == user_id)
                .scalar()
            )
        finally:
            status_db.close()
        return InitialSyncResponse(
            status=initial_response_status,
            imported_count=0,
            skipped_count=0,
            history_id=history_id,
            account_status="existing",
            sync_mode="incremental",
            stored_email_count=stored_email_count,
            incremental_sync_status=incremental_status,
        )
    except HTTPException:
        db.rollback()
        db.close()
        raise
    except Exception as exc:
        db.rollback()
        db.close()
        logger.exception("Unable to queue initial Gmail synchronization")
        raise HTTPException(
            status_code=502,
            detail="Unable to queue initial Gmail synchronization.",
        ) from exc


@router.get(
    "/emails/sync-initial/status",
    response_model=InitialSyncStatusResponse,
)
def get_initial_sync_status(
    background_tasks: BackgroundTasks,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, _ = _resolve_user_account(
        _get_bearer_token(authorization),
        db,
        timing_context="get_initial_sync_status",
    )
    db.commit()
    initial_sync_status = getattr(user, "initial_sync_status", None)
    if (
        user.initial_sync_eligible
        and initial_sync_status == "pending"
        and _claim_stale_initial_sync(db, user.id)
    ):
        db.commit()
        background_tasks.add_task(run_initial_gmail_sync, user.id)
    status = initial_sync_status or "not_started"
    account_status = (
        "new"
        if user.initial_sync_eligible
        and initial_sync_status in ("not_started", "pending", "running")
        else "existing"
    )
    sync_mode = "initial" if account_status == "new" else "incremental"
    stored_email_count = (
        db.query(func.count(Email.id))
        .filter(Email.user_id == user.id)
        .scalar()
        or 0
    )
    return InitialSyncStatusResponse(
        status=status,
        history_id=user.last_history_id,
        account_status=account_status,
        sync_mode=sync_mode,
        stored_email_count=stored_email_count,
        incremental_sync_status=user.incremental_sync_status,
    )


def _claim_incremental_sync(user_id: str, execution_id: str) -> bool:
    db: Session = SessionLocal()
    try:
        user = (
            db.query(UserAccount)
            .filter(UserAccount.id == user_id)
            .with_for_update()
            .first()
        )
        if user is None:
            return False
        user.incremental_sync_id = execution_id
        user.incremental_sync_status = "running"
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def process_realtime_email_update(
    email_address: str,
    pushed_history_id: str,
    force_sync: bool = False,
    execution_id: str | None = None,
):
    """Fetch Gmail changes without holding database connections or locks."""
    user_id = None
    owns_preclaimed_execution = execution_id is not None
    execution_id = execution_id or str(uuid.uuid4())
    db: Session = SessionLocal()
    try:
        user_data = (
            db.query(UserAccount)
            .filter(func.lower(UserAccount.email) == email_address.lower())
            .with_for_update()
            .first()
        )
        if not user_data or not user_data.access_token:
            logger.warning("No credentials found for incoming user %s", email_address)
            raise GmailReauthorizationRequired(
                "Reconnect your Google account to restore Gmail synchronization."
            )

        user_id = user_data.id
        access_token = user_data.access_token
        refresh_token = user_data.refresh_token
        token_expiry = user_data.token_expiry
        start_id = user_data.last_history_id
        if (
            execution_id
            and getattr(user_data, "incremental_sync_id", None) == execution_id
        ):
            pass
        elif owns_preclaimed_execution:
            return
        elif not force_sync and start_id and int(pushed_history_id) <= int(start_id):
            return
        else:
            user_data.incremental_sync_id = execution_id
            user_data.incremental_sync_status = "running"
            db.commit()
    finally:
        db.close()

    try:
        if not start_id:
            raise RuntimeError(
                "Gmail account has no saved history cursor; cannot safely process notification."
            )
        token_state = {"access_token": access_token, "expiry": token_expiry}

        def persist_refreshed_token(
            refreshed_access_token: str,
            expiry: datetime | None,
            refreshed_refresh_token: str | None,
        ) -> None:
            nonlocal refresh_token
            _persist_gmail_token_state(
                user_id,
                refreshed_access_token,
                expiry,
                refreshed_refresh_token,
                expected_access_token=token_state["access_token"],
            )
            token_state["access_token"] = refreshed_access_token
            token_state["expiry"] = expiry
            if refreshed_refresh_token and refreshed_refresh_token.strip():
                refresh_token = refreshed_refresh_token

        _, active_token, token_expiry = get_gmail_service_with_token_state(
            access_token,
            refresh_token,
            token_expiry,
            refresh_if_expiry_missing=bool(refresh_token),
            on_token_refresh=persist_refreshed_token,
        )
        try:
            new_ids, latest_id, active_token = fetch_new_message_ids_from_history(
                access_token=active_token,
                start_history_id=start_id,
                refresh_token=refresh_token,
                raise_on_expired=True,
                token_expiry=token_state["expiry"],
                on_token_refresh=persist_refreshed_token,
            )
            fetched_emails = []
            for msg_id in new_ids:
                email_data = fetch_single_email(
                    access_token=active_token,
                    message_id=msg_id,
                    refresh_token=refresh_token,
                    token_expiry=token_state["expiry"],
                    on_token_refresh=persist_refreshed_token,
                )
                active_token = email_data.get("access_token", active_token)
                fetched_emails.append(email_data)
            watch_expiration = None
        except GmailHistoryCursorExpired:
            (
                fetched_emails,
                latest_id,
                active_token,
                watch_expiration,
            ) = _recover_expired_gmail_history(
                access_token=active_token,
                refresh_token=refresh_token,
                token_expiry=token_state["expiry"],
                on_token_refresh=persist_refreshed_token,
            )
            token_state["access_token"] = active_token

        db = SessionLocal()
        try:
            user = (
                db.query(UserAccount)
                .filter(UserAccount.id == user_id)
                .with_for_update()
                .first()
            )
            if user is None:
                logger.warning(
                    "Account disappeared while processing Gmail update for %s",
                    email_address,
                )
                return
            if user.incremental_sync_id != execution_id:
                logger.info(
                    "Discarding stale Gmail sync result for user_id=%s",
                    user_id,
                )
                db.rollback()
                return
            for email_data in fetched_emails:
                if watch_expiration is not None:
                    _upsert_recovered_email(db, user, email_data)
                else:
                    _store_email_if_new(db, user, email_data)
            user.access_token = active_token
            user.token_expiry = token_state["expiry"]
            user.last_history_id = (
                _latest_history_id(user.last_history_id, latest_id)
                or start_id
            )
            if watch_expiration is not None:
                user.watch_expiration = watch_expiration
            user.incremental_sync_status = "completed"
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        logger.info(
            "Ingested %d new email(s) for %s up to historyId %s",
            len(fetched_emails),
            email_address,
            latest_id,
        )
        try:
            tag_recent_untagged_emails(user_id)
        except Exception:
            logger.exception(
                "Failed starting background tagging for user_id=%s", user_id
            )
    except Exception:
        if user_id is not None:
            try:
                _set_incremental_sync_status(
                    user_id,
                    "failed",
                    execution_id,
                )
            except Exception:
                logger.exception(
                    "Could not record incremental Gmail synchronization failure "
                    "for user_id=%s",
                    user_id,
                )
        raise


def _upsert_recovered_email(
    db: Session,
    user: UserAccount,
    email_data: dict,
) -> None:
    email_values = {
        "id": email_data["id"],
        "thread_id": email_data["thread_id"],
        "user_id": user.id,
        "sender": email_data["sender"],
        "subject": email_data["subject"],
        "preview": email_data["preview"],
        "body": email_data["body"],
        "body_html": email_data["body_html"],
        "has_html": email_data["has_html"],
        "created_at": email_data["received_at"],
        "is_read": bool(email_data.get("is_read", False)),
    }
    if db.get_bind().dialect.name == "postgresql":
        inserted = db.execute(
            postgresql_insert(Email)
            .values(**email_values)
            .on_conflict_do_update(
                index_elements=[Email.id],
                set_={
                    key: value
                    for key, value in email_values.items()
                    if key not in ("id", "user_id")
                },
                where=Email.user_id == user.id,
            )
        )
        if not inserted.rowcount:
            existing = db.query(Email).filter(Email.id == email_data["id"]).first()
            if existing and existing.user_id != user.id:
                logger.error(
                    "Gmail message ID collision across accounts; refusing recovery update"
                )
            return
        db.execute(
            postgresql_insert(EmailAgentStateModel)
            .values(
                email_id=email_data["id"],
                category=None,
                priority=None,
                is_archived=False,
            )
            .on_conflict_do_nothing(index_elements=[EmailAgentStateModel.email_id])
        )
        return

    existing = db.query(Email).filter(Email.id == email_data["id"]).first()
    if existing is not None:
        if existing.user_id != user.id:
            logger.error(
                "Gmail message ID collision across accounts; refusing recovery update"
            )
            return
        for key, value in email_values.items():
            if key not in ("id", "user_id"):
                setattr(existing, key, value)
        return

    _store_email_if_new(db, user, email_data)


def _recover_expired_gmail_history(
    access_token: str,
    refresh_token: str | None,
    token_expiry: datetime | None,
    on_token_refresh: TokenRefreshCallback,
) -> tuple[list[dict], str, str, str]:
    """Rebuild current inbox state, then catch changes after a fresh watch boundary."""
    watch = start_gmail_watch(
        access_token,
        refresh_token,
        token_expiry=token_expiry,
        on_token_refresh=on_token_refresh,
    )
    boundary = watch.get("historyId")
    expiration = watch.get("expiration")
    if not boundary or not expiration:
        raise RuntimeError("Gmail did not return a valid recovery watch boundary.")

    current_ids, active_token = list_message_ids(
        access_token=access_token,
        query="in:inbox",
        refresh_token=refresh_token,
        token_expiry=token_expiry,
        on_token_refresh=on_token_refresh,
    )
    changed_ids, latest_id, active_token = fetch_new_message_ids_from_history(
        access_token=active_token,
        start_history_id=str(boundary),
        refresh_token=refresh_token,
        raise_on_expired=True,
        token_expiry=token_expiry,
        on_token_refresh=on_token_refresh,
        history_types=[
            "messageAdded",
            "labelAdded",
            "labelRemoved",
        ],
    )
    fetched_emails = []
    for message_id in dict.fromkeys([*current_ids, *changed_ids]):
        email_data = fetch_single_email(
            access_token=active_token,
            message_id=message_id,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            on_token_refresh=on_token_refresh,
        )
        active_token = email_data.get("access_token", active_token)
        fetched_emails.append(email_data)
    return fetched_emails, latest_id, active_token, str(expiration)


def _set_incremental_sync_status(
    user_id: str,
    status: str,
    execution_id: str,
) -> bool:
    db: Session = SessionLocal()
    try:
        user = (
            db.query(UserAccount)
            .filter(UserAccount.id == user_id)
            .with_for_update()
            .first()
        )
        if user is None or user.incremental_sync_id != execution_id:
            return False
        user.incremental_sync_status = status
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/webhooks/gmail/pubsub")
def gmail_pubsub_webhook(req: PubSubPushRequest):
    """Processes a Cloud Pub/Sub push before acknowledging successful ingestion."""
    try:
        raw_data = base64.b64decode(req.message.data, validate=True).decode("utf-8")
        payload = json.loads(raw_data)
        if not isinstance(payload, dict):
            raise ValueError("Pub/Sub payload must be a JSON object")

        email_address = payload.get("emailAddress")
        history_id = payload.get("historyId")
        if not isinstance(email_address, str) or not email_address.strip():
            raise ValueError("Pub/Sub payload is missing emailAddress")
        if isinstance(history_id, bool) or not (
            isinstance(history_id, int)
            or isinstance(history_id, str)
            and history_id.isdigit()
        ):
            raise ValueError("Pub/Sub payload has an invalid historyId")
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        logger.warning("Malformed Pub/Sub message received")
        raise HTTPException(
            status_code=400,
            detail="Malformed Gmail notification.",
        ) from exc

    try:
        process_realtime_email_update(email_address, str(history_id))
    except Exception as exc:
        logger.exception("Failed processing Gmail Pub/Sub notification")
        raise HTTPException(
            status_code=503,
            detail="Gmail notification processing failed; retry the delivery.",
        ) from exc

    return Response(status_code=204)


@router.get("/emails")
def get_emails(
    is_read: Optional[bool] = None,
    unread: Optional[bool] = None,
    status: Optional[str] = None,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, _ = _resolve_user_account(
        _get_bearer_token(authorization),
        db,
        timing_context="get_emails",
    )
    db.commit()

    filter_read: Optional[bool] = None
    if is_read is not None:
        filter_read = is_read
    elif unread is not None:
        filter_read = not unread
    elif status is not None:
        if status.lower() == "read":
            filter_read = True
        elif status.lower() == "unread":
            filter_read = False

    query = (
        db.query(Email)
        .options(
            load_only(
                Email.id,
                Email.thread_id,
                Email.user_id,
                Email.sender,
                Email.subject,
                Email.preview,
                Email.category,
                Email.priority,
                Email.is_archived,
                Email.is_read,
                Email.created_at,
            ),
            joinedload(Email.state).load_only(
                EmailAgentStateModel.category,
                EmailAgentStateModel.priority,
                EmailAgentStateModel.is_archived,
            ),
        )
        .filter(Email.user_id == user.id)
    )

    if filter_read is not None:
        query = query.filter(Email.is_read == filter_read)

    emails = query.order_by(Email.created_at.desc()).all()
    response = []

    for e in emails:
        response.append(
            {
                "id": e.id,
                "thread_id": e.thread_id,
                "sender": e.sender,
                "subject": e.subject,
                "preview": e.preview or "",
                "received_at": e.created_at.isoformat() if e.created_at else None,
                "category": (e.state.category if e.state else e.category),
                "priority": (e.state.priority if e.state else e.priority),
                "is_archived": (
                    e.state.is_archived
                    if e.state and e.state.is_archived is not None
                    else bool(e.is_archived)
                ),
                "is_read": bool(e.is_read),
            }
        )

    return {"data": response}


def _set_email_read_state(
    email_id: str,
    is_read: bool,
    user: UserAccount,
    token: str,
    db: Session,
) -> Email:
    email = (
        db.query(Email)
        .filter(Email.id == email_id, Email.user_id == user.id)
        .first()
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    original_is_read = bool(email.is_read)
    email.is_read = is_read
    db.commit()

    try:
        update_gmail_message_read_status(
            access_token=token,
            message_id=email.id,
            is_read=is_read,
            refresh_token=user.refresh_token,
            token_expiry=user.token_expiry,
            on_token_refresh=lambda refreshed_access, expiry, refreshed_refresh: (
                _persist_gmail_token_state(
                    user.id,
                    refreshed_access,
                    expiry,
                    refreshed_refresh,
                    expected_access_token=token,
                )
            ),
        )
    except GmailReauthorizationRequired as exc:
        email.is_read = original_is_read
        db.commit()
        raise HTTPException(
            status_code=401,
            detail=(
                "Google Gmail access must be reauthorized. Reconnect your "
                "Google account and grant offline Gmail access."
            ),
        ) from exc
    except HttpError as exc:
        email.is_read = original_is_read
        db.commit()
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 403:
            raise HTTPException(
                status_code=403,
                detail="Gmail permission is missing. Reconnect your Google account.",
            ) from exc
        logger.exception("Unable to update Gmail labels for email %s", email_id)
        raise HTTPException(
            status_code=502,
            detail="Unable to synchronize read status with Gmail. Please try again.",
        ) from exc
    except Exception as exc:
        email.is_read = original_is_read
        db.commit()
        logger.exception("Unable to update Gmail labels for email %s", email_id)
        raise HTTPException(
            status_code=502,
            detail="Unable to synchronize read status with Gmail. Please try again.",
        ) from exc

    return email


@router.get("/emails/{email_id}/body")
def get_email_body(
    email_id: str,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    request_started = perf_counter()
    auth_started = perf_counter()
    user, active_token = _resolve_user_account(
        _get_bearer_token(authorization), db, timing_context="email_body"
    )
    auth_ms = (perf_counter() - auth_started) * 1000
    database_started = perf_counter()
    db.commit()
    email = (
        db.query(Email).filter(Email.id == email_id, Email.user_id == user.id).first()
    )
    database_ms = (perf_counter() - database_started) * 1000
    logger.info(
        "email_body_request_timing auth_ms=%.1f database_ms=%.1f total_ms=%.1f",
        auth_ms,
        database_ms,
        (perf_counter() - request_started) * 1000,
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    if not email.is_read:
        email.is_read = True
        db.commit()
        try:
            update_gmail_message_read_status(
                access_token=active_token,
                message_id=email.id,
                is_read=True,
                refresh_token=user.refresh_token,
                token_expiry=user.token_expiry,
                on_token_refresh=lambda refreshed_access, expiry, refreshed_refresh: (
                    _persist_gmail_token_state(
                        user.id,
                        refreshed_access,
                        expiry,
                        refreshed_refresh,
                        expected_access_token=active_token,
                    )
                ),
            )
        except Exception:
            logger.warning(
                "Failed to synchronize Gmail UNREAD label for opened email %s",
                email.id,
                exc_info=True,
            )

    return {
        "email_id": email.id,
        "body": email.body or "",
        "body_html": email.body_html or "",
    }


@router.get("/emails/{email_id}")
def get_email(
    email_id: str,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, active_token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email = (
        db.query(Email)
        .options(
            joinedload(Email.state),
        )
        .filter(Email.id == email_id, Email.user_id == user.id)
        .first()
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    if not email.is_read:
        email.is_read = True
        db.commit()
        try:
            update_gmail_message_read_status(
                access_token=active_token,
                message_id=email.id,
                is_read=True,
                refresh_token=user.refresh_token,
                token_expiry=user.token_expiry,
                on_token_refresh=lambda refreshed_access, expiry, refreshed_refresh: (
                    _persist_gmail_token_state(
                        user.id,
                        refreshed_access,
                        expiry,
                        refreshed_refresh,
                    )
                ),
            )
        except Exception:
            logger.warning(
                "Failed to synchronize Gmail UNREAD label for opened email %s",
                email.id,
                exc_info=True,
            )

    return {
        "id": email.id,
        "thread_id": email.thread_id,
        "sender": email.sender,
        "subject": email.subject,
        "preview": email.preview or "",
        "body": email.body or "",
        "body_html": email.body_html or "",
        "received_at": email.created_at.isoformat() if email.created_at else None,
        "category": (email.state.category if email.state else email.category),
        "priority": (email.state.priority if email.state else email.priority),
        "is_archived": (
            email.state.is_archived
            if email.state and email.state.is_archived is not None
            else bool(email.is_archived)
        ),
        "is_read": bool(email.is_read),
    }


@router.patch("/emails/{email_id}")
def patch_email_status(
    email_id: str,
    req: Optional[EmailReadUpdateRequest] = Body(None),
    is_read: Optional[bool] = Query(None),
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    target_read = True
    if req is not None and req.is_read is not None:
        target_read = req.is_read
    elif is_read is not None:
        target_read = is_read

    email = _set_email_read_state(
        email_id=email_id,
        is_read=target_read,
        user=user,
        token=token,
        db=db,
    )
    return {
        "status": "success",
        "email_id": email.id,
        "id": email.id,
        "is_read": email.is_read,
    }


@router.post("/emails/{email_id}/read")
@router.patch("/emails/{email_id}/read")
def mark_email_read(
    email_id: str,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email = _set_email_read_state(
        email_id=email_id,
        is_read=True,
        user=user,
        token=token,
        db=db,
    )
    return {
        "status": "success",
        "email_id": email.id,
        "id": email.id,
        "is_read": True,
    }


@router.post("/emails/{email_id}/unread")
@router.patch("/emails/{email_id}/unread")
def mark_email_unread(
    email_id: str,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email = _set_email_read_state(
        email_id=email_id,
        is_read=False,
        user=user,
        token=token,
        db=db,
    )
    return {
        "status": "success",
        "email_id": email.id,
        "id": email.id,
        "is_read": False,
    }


@action_router.post("/read")
def action_mark_read(
    req: EmailActionRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    target_read = True if req.is_read is None else req.is_read
    email = _set_email_read_state(
        email_id=req.email_id,
        is_read=target_read,
        user=user,
        token=token,
        db=db,
    )
    return {
        "status": "success",
        "email_id": email.id,
        "id": email.id,
        "is_read": email.is_read,
    }


@action_router.post("/unread")
def action_mark_unread(
    req: EmailActionRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email = _set_email_read_state(
        email_id=req.email_id,
        is_read=False,
        user=user,
        token=token,
        db=db,
    )
    return {
        "status": "success",
        "email_id": email.id,
        "id": email.id,
        "is_read": False,
    }


@action_router.post("/draft")
def generate_draft(
    req: EmailActionRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Stream a reply draft as server-sent token events."""
    user, _ = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email, _ = _get_email_action_state(req.email_id, user.id, db)
    action_state = _build_action_state(email)
    email_id = email.id
    db.close()

    def events():
        generated: list[str] = []
        try:
            for token in stream_draft_reply(action_state):
                generated.append(token)
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            persistence_db = SessionLocal()
            try:
                persisted_state = (
                    persistence_db.query(EmailAgentStateModel)
                    .filter(EmailAgentStateModel.email_id == email_id)
                    .first()
                )
                if persisted_state is None:
                    raise RuntimeError(
                        "Email triage state disappeared during drafting."
                    )
                persisted_state.reply_outline = []
                persisted_state.suggested_reply = "".join(generated)
                persistence_db.commit()
            finally:
                persistence_db.close()
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception:
            logger.exception("Reply generation failed for email %s", email_id)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Reply generation failed.'})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@action_router.post("/send")
def send_reply(
    req: EmailSendRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email = (
        db.query(Email)
        .filter(Email.id == req.email_id, Email.user_id == user.id)
        .first()
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    email_id = email.id
    thread_id = email.thread_id or email.id
    sender = email.sender
    subject = email.subject
    refresh_token = user.refresh_token
    token_expiry = user.token_expiry
    db.close()
    try:
        sent_id = send_email_reply(
            access_token=token,
            email_id=email_id,
            thread_id=thread_id,
            sender=sender,
            subject=subject,
            body=req.body,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            on_token_refresh=lambda refreshed_access, expiry, refreshed_refresh: (
                _persist_gmail_token_state(
                    user.id,
                    refreshed_access,
                    expiry,
                    refreshed_refresh,
                    expected_access_token=token,
                )
            ),
        )
        return {"status": "sent", "message_id": sent_id}
    # except ValueError as e:
    #     raise HTTPException(status_code=422, detail=str(e))
    # except Exception as e:
    #     logger.exception("Unable to send reply for email %s", email_id)
    #     raise HTTPException(status_code=502, detail=f"Unable to send reply: {str(e)}")
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=str(e),
        ) from e

    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)

        if status == 403:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Gmail permission is missing. Reconnect your Google "
                    "account and grant permission to send emails."
                ),
            ) from e

        logger.exception("Unable to send reply for email %s", email_id)
        raise HTTPException(
            status_code=502,
            detail="Unable to send your reply. Please try again.",
        ) from e

    except Exception as e:
        logger.exception("Unable to send reply for email %s", email_id)
        raise HTTPException(
            status_code=502,
            detail="Unable to send your reply. Please try again.",
        ) from e
