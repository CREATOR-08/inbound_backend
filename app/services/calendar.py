from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

CALENDAR_TIMEZONE = ZoneInfo("Asia/Kolkata")


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