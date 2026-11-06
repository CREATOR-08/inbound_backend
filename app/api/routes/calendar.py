
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.agent.nodes import scheduler_node
from app.api.dependencies import _get_bearer_token, _resolve_user_account
from app.api.routes.common import (
    _build_action_state,
    _get_email_action_state,
)
from app.api.schemas.schemas import CalendarApprovalRequest, EmailActionRequest
from app.db.database import SessionLocal, get_db
from app.db.models import CalendarDraft, Email
from app.services.calendar import (
    create_google_calendar_event,
    normalize_calendar_event_times,
)
from app.services.gmail import clean_text_for_llm, html_to_text

logger = logging.getLogger(__name__)
action_router = APIRouter()

SCHEDULE_AMBIGUITY_MESSAGE = (
    "The email does not provide complete, unambiguous event times. "
    "Enter the start and end times manually to continue."
)

CALENDAR_PERMISSION_MESSAGE = (
    "Google Calendar permission is missing. "
    "Reconnect your Google account and grant Calendar access."
)


def _schedule_proposal_times(
    event: dict,
) -> tuple[str, str, float, str | None]:
    """Validate and normalize proposed event times."""
    extraction_status = event.get("extraction_status")
    if extraction_status in {"missing", "ambiguous", "invalid"}:
        message = event.get("ambiguity")
        return "", "", 0.0, (
            message
            if isinstance(message, str) and message.strip()
            else "Please provide a complete, unambiguous meeting date, time, and duration."
        )

    confidence = float(event.get("confidence", 0.0))

    if confidence <= 0:
        return "", "", confidence, SCHEDULE_AMBIGUITY_MESSAGE

    start_time, end_time = normalize_calendar_event_times(
        event["start_time"],
        event["end_time"],
    )

    return start_time, end_time, confidence, None


@action_router.post("/schedule")
def generate_schedule(
    req: EmailActionRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """
    Extract a calendar proposal and save a draft.
    This endpoint does not create a Google Calendar event.
    """
    user, _token = _resolve_user_account(
        _get_bearer_token(authorization),
        db,
    )

    email, _ = _get_email_action_state(req.email_id, user.id, db)
    action_state = _build_action_state(email)
    email_body = (
        email.body.strip()
        if isinstance(email.body, str) and email.body.strip()
        else html_to_text(email.body_html or "")
    )
    if not clean_text_for_llm(email_body):
        raise HTTPException(
            status_code=422,
            detail="This email has no readable body. Meeting details cannot be extracted.",
        )
    action_state["body"] = email_body

    email_id = email.id
    email_subject = email.subject
    email_received_at = (
        email.created_at.isoformat() if email.created_at else None
    )

    # Avoid keeping the request's DB session open while the AI runs.
    db.close()

    # Generate the proposal exactly once.
    try:
        result = scheduler_node(action_state, email_received_at)

        event = result.get("calendar_event") if isinstance(result, dict) else None

        if not isinstance(event, dict):
            raise HTTPException(
                status_code=502,
                detail=(
                    "Schedule extraction failed to produce a valid "
                    "calendar proposal."
                ),
            )

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "Schedule generation failed for email %s",
            req.email_id,
        )
        raise HTTPException(
            status_code=502,
            detail="Unable to generate the meeting schedule. Please try again.",
        ) from exc

    extraction_status = event.get("extraction_status", "complete")
    try:
        start_time, end_time, confidence, ambiguity = (
            _schedule_proposal_times(event)
        )

    except (ValueError, KeyError, TypeError):
        logger.warning(
            "Schedule extraction returned invalid event data for email %s",
            req.email_id,
        )
        start_time, end_time, confidence = "", "", 0.0
        ambiguity = (
            "I couldn't validate the extracted meeting date or time. "
            "Please enter the date and time manually."
        )
        extraction_status = "invalid"
    if confidence <= 0 and extraction_status == "complete":
        extraction_status = "ambiguous"

    draft_db = SessionLocal()

    try:
        draft = (
            draft_db.query(CalendarDraft)
            .join(Email, Email.id == CalendarDraft.email_id)
            .filter(
                CalendarDraft.email_id == email_id,
                Email.user_id == user.id,
            )
            .first()
        )

        if draft and draft.status == "APPROVED":
            raise HTTPException(
                status_code=409,
                detail="A calendar event has already been approved for this email.",
            )

        title = event.get("title") or email_subject or "Meeting"
        attendees = event.get("attendees") or []

        if draft is None:
            draft = CalendarDraft(
                email_id=email_id,
                title=title,
                start_time=start_time,
                end_time=end_time,
                attendees=attendees,
                confidence=str(confidence),
                status="PENDING",
            )
            draft_db.add(draft)
        else:
            draft.title = title
            draft.start_time = start_time
            draft.end_time = end_time
            draft.attendees = attendees
            draft.confidence = str(confidence)
            draft.status = "PENDING"

        draft_db.commit()
        draft_db.refresh(draft)

        return {
            "confirmation_required": True,
            "calendar_event": {
                "draft_id": str(draft.id),
                "title": draft.title,
                "start_time": draft.start_time,
                "end_time": draft.end_time,
                "attendees": draft.attendees or [],
                "confidence": float(draft.confidence or 0),
                "ambiguity": ambiguity,
                "extraction_status": extraction_status,
                "status": draft.status,
            },
        }

    except HTTPException:
        draft_db.rollback()
        raise

    except Exception as exc:
        draft_db.rollback()
        logger.exception(
            "Failed to save calendar draft for email %s",
            req.email_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to save the calendar proposal. Please try again.",
        ) from exc

    finally:
        draft_db.close()


@action_router.post("/calendar/confirm")
def confirm_calendar_event(
    req: CalendarApprovalRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    """Create a Google Calendar event only after explicit user approval."""
    user, token = _resolve_user_account(
        _get_bearer_token(authorization),
        db,
    )

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
        raise HTTPException(
            status_code=404,
            detail="Draft not found.",
        )

    if draft.status == "APPROVED":
        return {
            "status": "already_approved",
            "google_event_id": draft.google_event_id,
        }

    title = req.title or draft.title
    start_time = req.start_time or draft.start_time
    end_time = req.end_time or draft.end_time

    if not title or not title.strip():
        raise HTTPException(
            status_code=422,
            detail="A title and ISO 8601 start_time/end_time are required.",
        )

    try:
        start_time, end_time = normalize_calendar_event_times(
            start_time,
            end_time,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    draft_id = draft.id
    attendees = (
        req.attendees
        if req.attendees is not None
        else draft.attendees
    )

    # Close the original session before making the external API request.
    db.close()

    try:
        created = create_google_calendar_event(
            access_token=token,
            title=title,
            start_time=start_time,
            end_time=end_time,
            attendees=attendees,
        )

        # Reopen a session to persist the result.
        update_db = SessionLocal()

        try:
            persisted_draft = (
                update_db.query(CalendarDraft)
                .join(Email, Email.id == CalendarDraft.email_id)
                .filter(
                    CalendarDraft.id == draft_id,
                    Email.user_id == user.id,
                )
                .first()
            )

            if persisted_draft is None:
                raise RuntimeError(
                    "Calendar draft disappeared after event creation."
                )

            persisted_draft.title = title
            persisted_draft.start_time = start_time
            persisted_draft.end_time = end_time
            persisted_draft.attendees = attendees
            persisted_draft.status = "APPROVED"
            persisted_draft.google_event_id = created.get("id")

            update_db.commit()
            google_event_id = persisted_draft.google_event_id

        except Exception:
            update_db.rollback()
            raise

        finally:
            update_db.close()

        return {
            "status": "success",
            "google_event_id": google_event_id,
        }

    except HTTPException:
        raise

    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)

        if status in (401, 403):
            logger.warning(
                "Google Calendar event creation denied with HTTP %s",
                status,
            )
            raise HTTPException(
                status_code=403,
                detail=CALENDAR_PERMISSION_MESSAGE,
            ) from exc

        logger.exception("Google Calendar API request failed")
        raise HTTPException(
            status_code=502,
            detail="Unable to create the calendar event. Please try again.",
        ) from exc

    except GoogleAuthError as exc:
        logger.warning("Google credentials failed during Calendar event creation")
        raise HTTPException(
            status_code=401,
            detail=(
                "Your Google access token is invalid or expired. "
                "Reconnect your Google account and try again."
            ),
        ) from exc

    except Exception as exc:
        logger.exception("Unable to confirm calendar event")
        raise HTTPException(
            status_code=500,
            detail="Unable to confirm the calendar event. Please try again.",
        ) from exc
