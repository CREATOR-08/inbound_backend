from typing import List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


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