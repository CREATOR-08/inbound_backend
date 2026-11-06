import logging

from sqlalchemy import func

from app.db.database import SessionLocal
from app.db.models import UserAccount
from app.jobs.reliability import log_job_failure

logger = logging.getLogger(__name__)


def run_initial_gmail_sync_job(user_id: str) -> None:
    from app.api.routes.emails import run_initial_gmail_sync

    try:
        run_initial_gmail_sync(user_id)
    except Exception as error:
        retryable, retries_left = log_job_failure(
            logger,
            "initial_gmail_sync",
            error,
            user_id=user_id,
        )
        if retryable and retries_left > 0:
            db = SessionLocal()
            try:
                db.query(UserAccount).filter(
                    UserAccount.id == user_id,
                    UserAccount.initial_sync_completed_at.is_(None),
                    UserAccount.initial_sync_status == "failed",
                ).update(
                    {
                        UserAccount.initial_sync_status: "pending",
                        UserAccount.initial_sync_eligible: True,
                        UserAccount.updated_at: func.now(),
                    },
                    synchronize_session=False,
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Could not restore initial sync state before retry for user_id=%s",
                    user_id,
                )
                raise
            finally:
                db.close()
        raise


def run_pubsub_gmail_sync_job(
    email_address: str,
    history_id: str,
    execution_id: str,
) -> None:
    from app.api.routes.emails import (
        _set_incremental_sync_status,
        process_realtime_email_update,
    )

    db = SessionLocal()
    try:
        user = (
            db.query(UserAccount)
            .filter(func.lower(UserAccount.email) == email_address.lower())
            .first()
        )
        if user is None or user.incremental_sync_id != execution_id:
            logger.info("Discarding stale Pub/Sub Gmail job for %s", email_address)
            return
        user_id = user.id
        missing_access_token = not bool(user.access_token)
    finally:
        db.close()

    try:
        if missing_access_token:
            raise RuntimeError(
                "The Gmail account has no access token for Pub/Sub synchronization."
            )
        process_realtime_email_update(
            email_address,
            history_id,
            execution_id=execution_id,
        )
    except Exception as error:
        try:
            retryable, retries_left = log_job_failure(
                logger,
                "pubsub_gmail_sync",
                error,
                user_id=user_id,
                history_id=history_id,
                execution_id=execution_id,
            )
            if retryable and retries_left > 0:
                _set_incremental_sync_status(user_id, "running", execution_id)
            else:
                _set_incremental_sync_status(user_id, "failed", execution_id)
        except Exception:
            logger.exception(
                "Could not record Pub/Sub sync failure for user_id=%s",
                user_id,
            )
        raise


def run_existing_account_catchup_job(
    user_id: str,
    email_address: str,
    execution_id: str,
) -> None:
    from app.api.routes.emails import (
        _register_user_gmail_watch,
        _set_incremental_sync_status,
        process_realtime_email_update,
    )

    db = SessionLocal()
    try:
        user = db.query(UserAccount).filter(UserAccount.id == user_id).first()
        if user is None or user.incremental_sync_id != execution_id:
            logger.info(
                "Discarding stale account catch-up job for user_id=%s",
                user_id,
            )
            return
        if not user.access_token:
            missing_access_token = True
            access_token = ""
        else:
            missing_access_token = False
            access_token = user.access_token
        watch_user = UserAccount(
            id=user.id,
            email=user.email,
            refresh_token=user.refresh_token,
            token_expiry=user.token_expiry,
            last_history_id=user.last_history_id,
            watch_expiration=user.watch_expiration,
        )
    finally:
        db.close()

    try:
        if missing_access_token:
            raise RuntimeError(
                "The Gmail account has no access token for incremental synchronization."
            )
        watch = _register_user_gmail_watch(watch_user, access_token)
        process_realtime_email_update(
            email_address,
            watch["historyId"],
            force_sync=True,
            execution_id=execution_id,
        )
    except Exception as error:
        try:
            retryable, retries_left = log_job_failure(
                logger,
                "existing_account_catchup",
                error,
                user_id=user_id,
                execution_id=execution_id,
            )
            if retryable and retries_left > 0:
                _set_incremental_sync_status(user_id, "running", execution_id)
            else:
                _set_incremental_sync_status(user_id, "failed", execution_id)
        except Exception:
            logger.exception(
                "Could not record account catch-up failure for user_id=%s",
                user_id,
            )
        raise


def run_incremental_recovery_job(
    user_id: str,
    email_address: str,
    execution_id: str,
) -> None:
    from app.api.routes.emails import (
        _register_user_gmail_watch,
        _set_incremental_sync_status,
        process_realtime_email_update,
    )

    db = SessionLocal()
    try:
        user = db.query(UserAccount).filter(UserAccount.id == user_id).first()
        if user is None or user.incremental_sync_id != execution_id:
            logger.info("Discarding stale incremental recovery for user_id=%s", user_id)
            return
        if not user.access_token:
            access_token = ""
            missing_access_token = True
        else:
            access_token = user.access_token
            missing_access_token = False
        history_id = user.last_history_id or ""
        watch_user = UserAccount(
            id=user.id,
            email=user.email,
            refresh_token=user.refresh_token,
            token_expiry=user.token_expiry,
            last_history_id=user.last_history_id,
            watch_expiration=user.watch_expiration,
        )
    finally:
        db.close()

    try:
        if missing_access_token:
            raise RuntimeError(
                "The Gmail account has no access token for incremental recovery."
            )
        watch = _register_user_gmail_watch(watch_user, access_token)
        process_realtime_email_update(
            email_address,
            history_id or watch["historyId"],
            force_sync=True,
            execution_id=execution_id,
        )
    except Exception as error:
        try:
            retryable, retries_left = log_job_failure(
                logger,
                "incremental_sync_recovery",
                error,
                user_id=user_id,
                execution_id=execution_id,
            )
            if retryable and retries_left > 0:
                _set_incremental_sync_status(user_id, "running", execution_id)
            else:
                _set_incremental_sync_status(user_id, "failed", execution_id)
        except Exception:
            logger.exception(
                "Could not record incremental recovery failure for user_id=%s",
                user_id,
            )
        raise
