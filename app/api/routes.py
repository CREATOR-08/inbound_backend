import base64
import json
import logging
from datetime import datetime, timezone
from time import perf_counter

from app.agent.nodes import scheduler_node, stream_draft_reply
from app.agent.graph import triage_graph
from app.agent.state import EmailAgentState
from app.api.schemas import (
    CalendarApprovalRequest,
    EmailActionRequest,
    EmailSendRequest,
    InitialSyncResponse,
    PubSubPushRequest,
)
from app.db.database import SessionLocal, get_db
from app.db.models import (
    CalendarDraft,
    Email,
    EmailAgentStateModel,
    UserAccount,
)
from app.services.calendar import create_google_calendar_event
from app.services.gmail import (
    fetch_new_message_ids_from_history,
    fetch_single_email,
    get_gmail_service_from_token,
    list_message_ids,
    send_email_reply,
    start_gmail_watch,
)
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Response,
)
from fastapi.responses import StreamingResponse
from googleapiclient.errors import HttpError
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload, load_only

logger = logging.getLogger(__name__)

VALID_CATEGORIES = ("MEETING_REQUEST", "ACTION_REQUIRED", "INFORMATIONAL", "SPAM")
VALID_PRIORITIES = ("HIGH", "MEDIUM", "LOW")

router = APIRouter()
action_router = APIRouter()

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(router)
api_v1_router.include_router(action_router, prefix="/actions")




def _get_bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid token header format")
    return token.strip()


def _get_email_action_state(
    email_id: str, user_id: str, db: Session
) -> tuple[Email, EmailAgentStateModel]:
    email = (
        db.query(Email)
        .filter(Email.id == email_id, Email.user_id == user_id)
        .first()
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    state = (
        db.query(EmailAgentStateModel)
        .filter(EmailAgentStateModel.email_id == email_id)
        .first()
    )
    if not state:
        raise HTTPException(status_code=409, detail="Email has not been triaged")

    return email, state


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


def _watch_is_active(expiration: str | None) -> bool:
    if not expiration:
        return False
    try:
        return int(expiration) > int(datetime.now(timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        return False


def _ensure_gmail_watch(
    user: UserAccount, access_token: str
) -> dict[str, str]:
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


def _store_email_if_new(
    db: Session, user: UserAccount, email_data: dict
) -> bool:
    existing = db.query(Email).filter(Email.id == email_data["id"]).first()
    if existing:
        if existing.user_id != user.id:
            logger.error(
                "Gmail message ID collision across accounts; refusing to reassign it"
            )
        return False

    db.add(
        Email(
            id=email_data["id"],
            thread_id=email_data["thread_id"],
            user_id=user.id,
            sender=email_data["sender"],
            subject=email_data["subject"],
            preview=email_data["preview"],
            body=email_data["body"],
            body_html=email_data["body_html"],
            has_html=email_data["has_html"],
            created_at=email_data["received_at"],
        )
    )
    db.add(
        EmailAgentStateModel(
            email_id=email_data["id"],
            category=None,
            priority=None,
            is_archived=False,
        )
    )
    return True


def _build_action_state(email: Email) -> EmailAgentState:
    return {
        "email_id": email.id,
        "sender": email.sender or "",
        "subject": email.subject or "",
        "body": email.body or email.preview or "",
        "category": None,
        "priority": None,
        "reply_outline": None,
        "suggested_reply": None,
        "calendar_event": None,
        "is_archived": False,
    }


def tag_recent_untagged_emails(user_id: str, limit: int = 10) -> None:
    """Tag up to limit recent untagged emails belonging to one user."""
    logger.info("Starting background tagging for user_id=%s limit=%s", user_id, limit)
    db: Session = SessionLocal()
    attempted_ids: set[str] = set()
    processed = 0
    updated = 0
    try:
        while processed < limit:
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
                .with_for_update(skip_locked=True, of=Email)
                .first()
            )
            if email is None:
                break

            processed += 1
            attempted_ids.add(email.id)
            email_id = email.id
            logger.info(
                "Tagging email_id=%s for user_id=%s (%s/%s)",
                email_id,
                user_id,
                processed,
                limit,
            )
            try:
                result = triage_graph.invoke(_build_action_state(email))
                category = result.get("category")
                priority = result.get("priority")
                if category not in VALID_CATEGORIES or priority not in VALID_PRIORITIES:
                    raise ValueError("Triage returned an invalid category or priority")

                state = email.state
                if state is None:
                    state = EmailAgentStateModel(email_id=email.id)
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
                logger.exception("Failed to tag email_id=%s for user_id=%s", email_id, user_id)
    except Exception:
        db.rollback()
        logger.exception("Failed selecting untagged emails for user_id=%s", user_id)
    finally:
        logger.info(
            "Background tagging complete for user_id=%s processed=%s updated=%s",
            user_id,
            processed,
            updated,
        )
        db.close()


# =========================================================================
# 1. Pub/Sub Watch Registration & Webhook
# =========================================================================


@router.post("/emails/watch")
def register_watch(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Registers Gmail to publish inbox events to Pub/Sub and saves initial historyId."""
    token = _get_bearer_token(authorization)

    try:
        user, active_token = _resolve_user_account(token, db, create=True)
        user = (
            db.query(UserAccount)
            .filter(UserAccount.id == user.id)
            .with_for_update()
            .one()
        )
        watch_res = _ensure_gmail_watch(user, active_token)
        db.commit()
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

    try:
        user, active_token = _resolve_user_account(token, db, create=True)
        user = (
            db.query(UserAccount)
            .filter(UserAccount.id == user.id)
            .with_for_update()
            .one()
        )
        if user.initial_sync_completed_at:
            history_id = user.last_history_id or ""
            db.commit()
            return InitialSyncResponse(
                status="already_completed",
                imported_count=0,
                skipped_count=0,
                history_id=history_id,
            )

        _ensure_gmail_watch(user, active_token)
        db.commit()

        user = (
            db.query(UserAccount)
            .filter(UserAccount.id == user.id)
            .with_for_update()
            .one()
        )
        if user.initial_sync_completed_at:
            history_id = user.last_history_id or ""
            db.commit()
            return InitialSyncResponse(
                status="already_completed",
                imported_count=0,
                skipped_count=0,
                history_id=history_id,
            )

        start_history_id = user.last_history_id
        if not start_history_id:
            raise RuntimeError("Gmail watch did not save a history cursor.")

        listed_ids, active_token = list_message_ids(
            access_token=active_token,
            query="in:inbox newer_than:3d",
            refresh_token=user.refresh_token,
        )
        changed_ids, latest_history_id, active_token = (
            fetch_new_message_ids_from_history(
                access_token=active_token,
                start_history_id=start_history_id,
                refresh_token=user.refresh_token,
                raise_on_expired=True,
            )
        )

        message_ids = list(dict.fromkeys([*listed_ids, *changed_ids]))
        imported_count = 0
        for message_id in message_ids:
            existing = (
                db.query(Email.id).filter(Email.id == message_id).first()
            )
            if existing:
                continue

            email_data = fetch_single_email(
                access_token=active_token,
                message_id=message_id,
                refresh_token=user.refresh_token,
            )
            active_token = email_data.get("access_token", active_token)
            if _store_email_if_new(db, user, email_data):
                imported_count += 1

        user.access_token = active_token
        user.last_history_id = (
            _latest_history_id(user.last_history_id, latest_history_id)
            or start_history_id
        )
        user.initial_sync_completed_at = datetime.now(timezone.utc)
        db.commit()
        background_tasks.add_task(tag_recent_untagged_emails, user.id)
        return InitialSyncResponse(
            status="completed",
            imported_count=imported_count,
            skipped_count=len(message_ids) - imported_count,
            history_id=user.last_history_id,
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Initial Gmail synchronization failed")
        raise HTTPException(
            status_code=502, detail="Initial Gmail synchronization failed."
        ) from e


def process_realtime_email_update(email_address: str, pushed_history_id: str):
    """Background worker: fetches delta messages from Gmail and stores them in PostgreSQL."""
    db: Session = SessionLocal()
    try:
        user = (
            db.query(UserAccount)
            .filter(func.lower(UserAccount.email) == email_address.lower())
            .with_for_update()
            .first()
        )
        if not user or not user.access_token:
            logger.warning("No credentials found for incoming user %s", email_address)
            return

        start_id = user.last_history_id or pushed_history_id

        new_ids, latest_id, active_token = fetch_new_message_ids_from_history(
            access_token=user.access_token,
            start_history_id=start_id,
            refresh_token=user.refresh_token,
        )

        user.access_token = active_token
        user.last_history_id = _latest_history_id(user.last_history_id, latest_id)

        for msg_id in new_ids:
            if (
                db.query(Email.id)
                .filter(Email.id == msg_id, Email.user_id == user.id)
                .first()
            ):
                continue

            email_data = fetch_single_email(
                access_token=active_token,
                message_id=msg_id,
                refresh_token=user.refresh_token,
            )

            _store_email_if_new(db, user, email_data)

        db.commit()
        tag_recent_untagged_emails(user.id)
        logger.info(
            "Ingested %d new email(s) for %s up to historyId %s",
            len(new_ids),
            email_address,
            latest_id,
        )
    except Exception:
        db.rollback()
        logger.exception("Failed processing Pub/Sub delta for %s", email_address)
    finally:
        db.close()


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


# =========================================================================
# 2. Email Query & Body Endpoints
# =========================================================================


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
        response.append({
            "id": e.id,
            "thread_id": e.thread_id,
            "sender": e.sender,
            "subject": e.subject,
            "preview": e.preview or "",
            "received_at": e.created_at.isoformat() if e.created_at else None,
            "category": (
                e.state.category if e.state else e.category
            ),
            "priority": (
                e.state.priority if e.state else e.priority
            ),
            "is_archived": (
                e.state.is_archived
                if e.state and e.state.is_archived is not None
                else bool(e.is_archived)
            ),
        })

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
        db.query(Email)
        .filter(Email.id == email_id, Email.user_id == user.id)
        .first()
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


# =========================================================================
# 3. Actions: Drafting, Sending, Scheduling
# =========================================================================


@action_router.post("/draft")
def generate_draft(
    req: EmailActionRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Stream a reply draft as server-sent token events."""
    user, _ = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email, state = _get_email_action_state(req.email_id, user.id, db)

    def events():
        generated: list[str] = []
        try:
            for token in stream_draft_reply(_build_action_state(email)):
                generated.append(token)
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            state.reply_outline = []
            state.suggested_reply = "".join(generated)
            db.commit()
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception:
            db.rollback()
            logger.exception("Reply generation failed for email %s", email.id)
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
    try:
        sent_id = send_email_reply(
            access_token=token,
            email_id=email.id,
            thread_id=email.thread_id or email.id,
            sender=email.sender,
            subject=email.subject,
            body=req.body,
            refresh_token=user.refresh_token,
        )
        db.commit()
        return {"status": "sent", "message_id": sent_id}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Unable to send reply for email %s", email.id)
        raise HTTPException(status_code=502, detail=f"Unable to send reply: {str(e)}")


@action_router.post("/schedule")
def generate_schedule(
    req: EmailActionRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Extract a calendar proposal for user review without creating an event."""
    user, _ = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()
    email, _ = _get_email_action_state(req.email_id, user.id, db)
    result = scheduler_node(_build_action_state(email))
    event = result["calendar_event"]

    draft = (
        db.query(CalendarDraft)
        .filter(CalendarDraft.email_id == email.id)
        .first()
    )
    if draft and draft.status == "APPROVED":
        raise HTTPException(
            status_code=409,
            detail="A calendar event has already been approved for this email",
        )

    title = event.get("title") or email.subject or "Meeting"
    if draft is None:
        draft = CalendarDraft(
            email_id=email.id,
            title=title,
            start_time=event["start_time"],
            end_time=event["end_time"],
            attendees=event.get("attendees") or [],
            confidence=str(event.get("confidence", 0.0)),
            status="PENDING",
        )
        db.add(draft)
    else:
        draft.title = title
        draft.start_time = event["start_time"]
        draft.end_time = event["end_time"]
        draft.attendees = event.get("attendees") or []
        draft.confidence = str(event.get("confidence", 0.0))
        draft.status = "PENDING"

    db.commit()
    db.refresh(draft)

    return {
        "confirmation_required": True,
        "calendar_event": {
            "draft_id": str(draft.id),
            "title": draft.title,
            "start_time": draft.start_time,
            "end_time": draft.end_time,
            "attendees": draft.attendees or [],
            "confidence": float(draft.confidence or 0),
            "status": draft.status,
        },
    }


@action_router.post("/calendar/confirm")
def confirm_calendar_event(
    req: CalendarApprovalRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """HITL Gate: Pushes the drafted event to Google Calendar once user approves."""
    user, token = _resolve_user_account(_get_bearer_token(authorization), db)
    db.commit()

    draft = (
        db.query(CalendarDraft)
        .join(Email, Email.id == CalendarDraft.email_id)
        .filter(
            CalendarDraft.id == req.draft_id,
            Email.user_id == user.id,
        )
        .first()
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    if draft.status == "APPROVED":
        return {"status": "already_approved", "google_event_id": draft.google_event_id}

    title = req.title or draft.title
    start_time = req.start_time or draft.start_time
    end_time = req.end_time or draft.end_time

    if not title.strip() or "T" not in start_time or "T" not in end_time:
        raise HTTPException(
            status_code=422,
            detail="A title and ISO 8601 start_time/end_time are required",
        )
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="A valid ISO 8601 start_time and end_time are required",
        )
    if (start.utcoffset() is None) != (end.utcoffset() is None):
        raise HTTPException(
            status_code=422,
            detail="The calendar event start_time and end_time must use compatible timezones",
        )
    if end <= start:
        raise HTTPException(
            status_code=422,
            detail="The calendar event end_time must be after start_time",
        )

    try:
        draft.title = title
        draft.start_time = start_time
        draft.end_time = end_time
        draft.attendees = req.attendees if req.attendees is not None else draft.attendees
        created = create_google_calendar_event(
            access_token=token,
            title=draft.title,
            start_time=draft.start_time,
            end_time=draft.end_time,
            attendees=draft.attendees,
        )

        draft.status = "APPROVED"
        draft.google_event_id = created.get("id")
        db.commit()

        return {"status": "success", "google_event_id": draft.google_event_id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Google Calendar Error: {str(e)}")