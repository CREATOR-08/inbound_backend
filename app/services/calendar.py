import logging
from datetime import datetime
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.db.database import SessionLocal
from app.db.models import CalendarDraft, Email

logger = logging.getLogger(__name__)
CALENDAR_TIMEZONE = ZoneInfo("Asia/Kolkata")
SCHEDULE_AMBIGUITY_MESSAGE = (
    "The email does not provide complete, unambiguous event times. "
    "Enter the start and end times manually to continue."
)


class CalendarDraftAlreadyApprovedError(ValueError):
  """Raised when an existing approved calendar draft cannot be replaced."""


def _schedule_proposal_times(
    event: dict[str, Any],
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


def save_calendar_draft(
    email_id: str,
    user_id: str,
    subject: str,
    event: dict[str, Any],
) -> dict[str, Any]:
  """Save an authenticated user's pending calendar proposal for later approval."""
  extraction_status = event.get("extraction_status", "complete")
  try:
    start_time, end_time, confidence, ambiguity = _schedule_proposal_times(event)
  except (ValueError, KeyError, TypeError):
    logger.warning(
        "Schedule extraction returned invalid event data for email %s",
        email_id,
    )
    start_time, end_time, confidence = "", "", 0.0
    ambiguity = (
        "I couldn't validate the extracted meeting date or time. "
        "Please enter the date and time manually."
    )
    extraction_status = "invalid"
  if confidence <= 0 and extraction_status == "complete":
    extraction_status = "ambiguous"

  db = SessionLocal()
  try:
    email = (
        db.query(Email)
        .filter(Email.id == email_id, Email.user_id == user_id)
        .first()
    )
    if email is None:
      raise LookupError("Email not found for the authenticated account.")

    draft = (
        db.query(CalendarDraft)
        .filter(CalendarDraft.email_id == email_id)
        .first()
    )
    if draft and draft.status == "APPROVED":
      raise CalendarDraftAlreadyApprovedError(
          "A calendar event has already been approved for this email."
      )

    title = event.get("title") or subject or "Meeting"
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
      db.add(draft)
    else:
      draft.title = title
      draft.start_time = start_time
      draft.end_time = end_time
      draft.attendees = attendees
      draft.confidence = str(confidence)
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
            "ambiguity": ambiguity,
            "extraction_status": extraction_status,
            "status": draft.status,
        },
    }
  except Exception:
    db.rollback()
    raise
  finally:
    db.close()


def normalize_calendar_event_times(start_time: str, end_time: str) -> tuple[str, str]:
  """Return RFC 3339 event times in Asia/Kolkata without changing their instants."""
  try:
    start = _normalize_calendar_timestamp(start_time)
    end = _normalize_calendar_timestamp(end_time)
  except (TypeError, ValueError) as exc:
    raise ValueError(
        "A valid ISO 8601 start_time and end_time are required"
    ) from exc

  if end <= start:
    raise ValueError("The calendar event end_time must be after start_time")

  return start.isoformat(), end.isoformat()


def _normalize_calendar_timestamp(value: str) -> datetime:
  if "T" not in value:
    raise ValueError("A time component is required")

  iso_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
  parsed = datetime.fromisoformat(iso_value)
  if parsed.utcoffset() is None:
    parsed = parsed.replace(tzinfo=CALENDAR_TIMEZONE)
  return parsed.astimezone(CALENDAR_TIMEZONE)


def get_calendar_service(access_token: str):
  """Builds a Google Calendar API client using the user's active access token."""
  creds = Credentials(token=access_token)
  return build("calendar", "v3", credentials=creds)


def create_google_calendar_event(
    access_token: str,
    title: str,
    start_time: str,
    end_time: str,
    attendees: Optional[List[str]] = None,
    description: Optional[str] = None,
) -> dict:
  """Creates a confirmed event in the user's primary Google Calendar."""
  start_time, end_time = normalize_calendar_event_times(start_time, end_time)
  service = get_calendar_service(access_token)

  event_payload = {
      "summary": title,
      "description": description or "Scheduled via MailFlow AI",
      "start": {
          "dateTime": start_time,
          "timeZone": "Asia/Kolkata",
      },
      "end": {
          "dateTime": end_time,
          "timeZone": "Asia/Kolkata",
      },
  }

  if attendees:
    event_payload["attendees"] = [{"email": email} for email in attendees]

  created_event = (
      service.events().insert(calendarId="primary", body=event_payload).execute()
  )
  return created_event