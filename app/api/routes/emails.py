import base64
import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from googleapiclient.errors import HttpError
from app.agent.graph import triage_graph
from app.agent.nodes import stream_draft_reply
from app.api.dependencies import _get_bearer_token, _resolve_user_account
from app.api.routes.common import (
    VALID_CATEGORIES,
    VALID_PRIORITIES,
    _build_action_state,
    _get_email_action_state,
)
from app.api.schemas.schemas import (
    EmailActionRequest,
    EmailSendRequest,
    InitialSyncResponse,
    PubSubPushRequest,
)
from app.db.database import SessionLocal, get_db
from app.db.models import Email, EmailAgentStateModel, UserAccount
from app.services.gmail import (
    fetch_new_message_ids_from_history,
    fetch_single_email,
    list_message_ids,
    send_email_reply,
    start_gmail_watch,
)
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, joinedload, load_only

logger = logging.getLogger(__name__)

router = APIRouter()
action_router = APIRouter()


def _watch_is_active(expiration: str | None) -> bool:
    if not expiration:
        return False
    try:
        return int(expiration) > int(datetime.now(timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        return False


def _ensure_gmail_watch(user: UserAccount, access_token: str) -> dict[str, str]:
    if user.last_history_id and _watch_is_active(user.watch_expiration):
        return {
            "historyId": user.last_history_id,
            "expiration": user.watch_expiration or "",
        }

    result = start_gmail_watch(access_token, user.refresh_token)
    history_id = result.get("historyId")
    expiration = result.get("expiration")
    if not history_id or not expiration:
        raise RuntimeError("Gmail did not return a valid watch cursor.")

    user.last_history_id = str(history_id)
    user.watch_expiration = str(expiration)
    return {"historyId": str(history_id), "expiration": str(expiration)}


def _latest_history_id(*history_ids: str | None) -> str | None:
    valid_ids = [history_id for history_id in history_ids if history_id]
    return max(valid_ids, key=int) if valid_ids else None


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
        db.commit()
        user_id = user.id
        watch_user = UserAccount(
            id=user_id,
            refresh_token=user.refresh_token,
            last_history_id=user.last_history_id,
            watch_expiration=user.watch_expiration,
        )
        db.close()
        watch_res = _ensure_gmail_watch(watch_user, active_token)
        storage_db = SessionLocal()
        try:
            stored_user = (
                storage_db.query(UserAccount).filter(UserAccount.id == user_id).one()
            )
            stored_user.last_history_id = watch_res["historyId"]
            stored_user.watch_expiration = watch_res["expiration"]
            storage_db.commit()
        finally:
            storage_db.close()
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


@router.post("/emails/sync-initial", response_model=InitialSyncResponse)
def sync_initial_emails(
    background_tasks: BackgroundTasks,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Imports the last three days once, then applies Gmail changes since the watch cursor."""
    token = _get_bearer_token(authorization)
    storage_db: Session | None = None

    try:
        user, active_token = _resolve_user_account(token, db, create=True)
        user_id = user.id
        refresh_token = user.refresh_token
        if user.initial_sync_completed_at:
            history_id = user.last_history_id or ""
            db.close()
            return InitialSyncResponse(
                status="already_completed",
                imported_count=0,
                skipped_count=0,
                history_id=history_id,
            )

        watch_user = UserAccount(
            id=user_id,
            refresh_token=refresh_token,
            last_history_id=user.last_history_id,
            watch_expiration=user.watch_expiration,
        )
        db.close()
        _ensure_gmail_watch(watch_user, active_token)
        start_history_id = watch_user.last_history_id
        if not start_history_id:
            raise RuntimeError("Gmail watch did not save a history cursor.")

        storage_db = SessionLocal()
        stored_user = (
            storage_db.query(UserAccount).filter(UserAccount.id == user_id).one()
        )
        stored_user.last_history_id = watch_user.last_history_id
        stored_user.watch_expiration = watch_user.watch_expiration
        storage_db.commit()
        storage_db.close()
        storage_db = None

        listed_ids, active_token = list_message_ids(
            access_token=active_token,
            query="in:inbox newer_than:3d",
            refresh_token=refresh_token,
        )
        changed_ids, latest_history_id, active_token = (
            fetch_new_message_ids_from_history(
                access_token=active_token,
                start_history_id=start_history_id,
                refresh_token=refresh_token,
                raise_on_expired=True,
            )
        )

        message_ids = list(dict.fromkeys([*listed_ids, *changed_ids]))
        fetched_emails = []
        imported_count = 0
        for message_id in message_ids:
            email_data = fetch_single_email(
                access_token=active_token,
                message_id=message_id,
                refresh_token=refresh_token,
            )
            active_token = email_data.get("access_token", active_token)
            fetched_emails.append(email_data)

        storage_db = SessionLocal()
        user = storage_db.query(UserAccount).filter(UserAccount.id == user_id).one()
        if user.initial_sync_completed_at:
            history_id = user.last_history_id or ""
            storage_db.close()
            return InitialSyncResponse(
                status="already_completed",
                imported_count=0,
                skipped_count=0,
                history_id=history_id,
            )
        for email_data in fetched_emails:
            if _store_email_if_new(storage_db, user, email_data):
                imported_count += 1
        user.access_token = active_token
        user.last_history_id = (
            _latest_history_id(user.last_history_id, latest_history_id)
            or start_history_id
        )
        user.initial_sync_completed_at = datetime.now(timezone.utc)
        storage_db.commit()
        history_id = user.last_history_id
        storage_db.close()
        storage_db = None
        background_tasks.add_task(tag_recent_untagged_emails, user.id)
        return InitialSyncResponse(
            status="completed",
            imported_count=imported_count,
            skipped_count=len(message_ids) - imported_count,
            history_id=history_id,
        )
    except HTTPException:
        if storage_db is not None:
            storage_db.rollback()
            storage_db.close()
        db.rollback()
        raise
    except Exception as e:
        if storage_db is not None:
            storage_db.rollback()
            storage_db.close()
        db.rollback()
        logger.exception("Initial Gmail synchronization failed")
        raise HTTPException(
            status_code=502, detail="Initial Gmail synchronization failed."
        ) from e


def process_realtime_email_update(email_address: str, pushed_history_id: str):
    """Fetch Gmail changes without holding database connections or locks."""
    db: Session = SessionLocal()
    try:
        user_data = (
            db.query(UserAccount)
            .filter(func.lower(UserAccount.email) == email_address.lower())
            .first()
        )
        if not user_data or not user_data.access_token:
            logger.warning("No credentials found for incoming user %s", email_address)
            return

        user_id = user_data.id
        access_token = user_data.access_token
        refresh_token = user_data.refresh_token
        start_id = user_data.last_history_id or pushed_history_id
    finally:
        db.close()

    try:
        new_ids, latest_id, active_token = fetch_new_message_ids_from_history(
            access_token=access_token,
            start_history_id=start_id,
            refresh_token=refresh_token,
        )
        fetched_emails = []
        for msg_id in new_ids:
            email_data = fetch_single_email(
                access_token=active_token,
                message_id=msg_id,
                refresh_token=refresh_token,
            )
            active_token = email_data.get("access_token", active_token)
            fetched_emails.append(email_data)
    except Exception:
        logger.exception("Failed fetching Gmail delta for %s", email_address)
        return

    db = SessionLocal()
    try:
        user = db.query(UserAccount).filter(UserAccount.id == user_id).first()
        if user is None:
            logger.warning(
                "Account disappeared while processing Gmail update for %s",
                email_address,
            )
            return
        user.access_token = active_token
        user.last_history_id = _latest_history_id(user.last_history_id, latest_id)
        for email_data in fetched_emails:
            _store_email_if_new(db, user, email_data)
        db.commit()
        logger.info(
            "Ingested %d new email(s) for %s up to historyId %s",
            len(fetched_emails),
            email_address,
            latest_id,
        )
    except Exception:
        db.rollback()
        logger.exception("Failed storing Gmail delta for %s", email_address)
        return
    finally:
        db.close()

    try:
        tag_recent_untagged_emails(user_id)
    except Exception:
        logger.exception("Failed starting background tagging for user_id=%s", user_id)


@router.post("/webhooks/gmail/pubsub")
async def gmail_pubsub_webhook(
    req: PubSubPushRequest, background_tasks: BackgroundTasks
):
    """Receives Cloud Pub/Sub pushes and delegates ingestion to a background worker."""
    try:
        raw_data = base64.b64decode(req.message.data).decode("utf-8")
        payload = json.loads(raw_data)

        email_address = payload.get("emailAddress")
        history_id = payload.get("historyId")

        if email_address and history_id:
            background_tasks.add_task(
                process_realtime_email_update, email_address, str(history_id)
            )
    except Exception:
        logger.exception("Malformed Pub/Sub message received")

    return Response(status_code=204)


@router.get("/emails")
def get_emails(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    user, _ = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    emails = (
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
                Email.created_at,
            ),
            joinedload(Email.state).load_only(
                EmailAgentStateModel.category,
                EmailAgentStateModel.priority,
                EmailAgentStateModel.is_archived,
            ),
        )
        .filter(Email.user_id == user.id)
        .order_by(Email.created_at.desc())
        .all()
    )
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
            }
        )

    return {"data": response}


@router.get("/emails/{email_id}/body")
def get_email_body(
    email_id: str,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    request_started = perf_counter()
    auth_started = perf_counter()
    user, _ = _resolve_user_account(
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

    return {
        "email_id": email.id,
        "body": email.body or "",
        "body_html": email.body_html or "",
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
