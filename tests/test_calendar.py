import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.agent.nodes import scheduler_node
from app.agent.state import CalendarEventDraft
from app.api.routes import calendar as calendar_routes
from app.api.schemas.schemas import CalendarApprovalRequest
from app.services.calendar import (
    create_google_calendar_event,
    normalize_calendar_event_times,
)
from app.api.routes.calendar import _schedule_proposal_times


class CalendarTimestampTests(unittest.TestCase):
    def test_schedule_extraction_receives_email_received_timestamp(self):
        chain = MagicMock()
        prompt = MagicMock()
        prompt.__or__.return_value = chain
        chain.invoke.return_value = CalendarEventDraft(
            title="Session",
            start_time="",
            end_time="",
            confidence=0.0,
        )

        with (
            patch("app.agent.nodes.ChatPromptTemplate.from_messages", return_value=prompt),
            patch("app.agent.nodes.llm"),
        ):
            result = scheduler_node(
                {
                    "email_id": "email-1",
                    "sender": "sender@example.com",
                    "subject": "Session",
                    "body": "Let's meet next Monday.",
                    "category": None,
                    "priority": None,
                    "reply_outline": None,
                    "suggested_reply": None,
                    "calendar_event": None,
                    "is_archived": False,
                },
                "2026-10-28T15:00:00+00:00",
            )

        chain.invoke.assert_called_once()
        self.assertEqual(
            chain.invoke.call_args.args[0]["email_received_at"],
            "2026-10-28T15:00:00+00:00",
        )
        self.assertEqual(result["calendar_event"]["confidence"], 0.0)

    def test_ambiguous_schedule_does_not_keep_model_generated_timestamps(self):
        start_time, end_time, confidence, ambiguity = _schedule_proposal_times(
            {
                "confidence": 0.0,
                "start_time": "2026-03-30T00:00:00+05:30",
                "end_time": "2026-03-30T01:00:00+05:30",
            }
        )

        self.assertEqual((start_time, end_time, confidence), ("", "", 0.0))
        self.assertEqual(
            ambiguity,
            "The email does not provide complete, unambiguous event times. "
            "Enter the start and end times manually to continue.",
        )

    def test_confident_schedule_proposal_is_normalized_to_ist(self):
        self.assertEqual(
            _schedule_proposal_times(
                {
                    "confidence": 0.8,
                    "start_time": "2026-10-31T14:35:00Z",
                    "end_time": "2026-10-31T15:35:00Z",
                }
            ),
            (
                "2026-10-31T20:05:00+05:30",
                "2026-10-31T21:05:00+05:30",
                0.8,
                None,
            ),
        )

    def test_explicit_ist_offset_is_preserved(self):
        self.assertEqual(
            normalize_calendar_event_times(
                "2026-10-31T20:05:00+05:30",
                "2026-10-31T21:05:00+05:30",
            ),
            ("2026-10-31T20:05:00+05:30", "2026-10-31T21:05:00+05:30"),
        )

    def test_utc_timestamp_is_converted_to_ist_without_changing_instant(self):
        start, end = normalize_calendar_event_times(
            "2026-10-31T14:35:00Z",
            "2026-10-31T15:35:00Z",
        )
        self.assertEqual(start, "2026-10-31T20:05:00+05:30")
        self.assertEqual(end, "2026-10-31T21:05:00+05:30")

    def test_datetime_local_value_is_interpreted_as_ist(self):
        self.assertEqual(
            normalize_calendar_event_times(
                "2026-10-31T20:05",
                "2026-10-31T21:05",
            ),
            ("2026-10-31T20:05:00+05:30", "2026-10-31T21:05:00+05:30"),
        )

    def test_conversion_crosses_midnight_and_year_boundary(self):
        self.assertEqual(
            normalize_calendar_event_times(
                "2026-12-31T20:05:00-05:00",
                "2026-12-31T21:05:00-05:00",
            ),
            ("2027-01-01T06:35:00+05:30", "2027-01-01T07:35:00+05:30"),
        )

    def test_aware_start_and_end_are_compared_as_instants(self):
        self.assertEqual(
            normalize_calendar_event_times(
                "2026-10-31T20:05:00+05:30",
                "2026-10-31T15:35:00Z",
            ),
            ("2026-10-31T20:05:00+05:30", "2026-10-31T21:05:00+05:30"),
        )

    def test_calendar_payload_uses_canonical_ist_rfc3339_values(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {
            "id": "event-1"
        }

        with patch(
            "app.services.calendar.get_calendar_service",
            return_value=service,
        ):
            result = create_google_calendar_event(
                access_token="token",
                title="Session",
                start_time="2026-10-31T14:35:00Z",
                end_time="2026-10-31T15:35:00Z",
            )

        self.assertEqual(result, {"id": "event-1"})
        service.events.return_value.insert.assert_called_once_with(
            calendarId="primary",
            body={
                "summary": "Session",
                "description": "Scheduled via MailFlow AI",
                "start": {
                    "dateTime": "2026-10-31T20:05:00+05:30",
                    "timeZone": "Asia/Kolkata",
                },
                "end": {
                    "dateTime": "2026-10-31T21:05:00+05:30",
                    "timeZone": "Asia/Kolkata",
                },
            },
        )

    def test_end_time_must_be_after_start_time(self):
        with self.assertRaisesRegex(ValueError, "must be after start_time"):
            normalize_calendar_event_times(
                "2026-10-31T20:05:00+05:30",
                "2026-10-31T14:35:00Z",
            )

    def test_confirmation_rejects_end_time_not_after_start_with_422(self):
        draft = SimpleNamespace(
            id="draft-1",
            status="PENDING",
            google_event_id=None,
            title="Session",
            start_time="2026-10-31T20:05:00+05:30",
            end_time="2026-10-31T21:05:00+05:30",
            attendees=[],
        )

        class FakeQuery:
            def join(self, *_args):
                return self

            def filter(self, *_args):
                return self

            def first(self):
                return draft

        class FakeDatabase:
            def commit(self):
                pass

            def query(self, *_args):
                return FakeQuery()

        request = CalendarApprovalRequest(
            draft_id="draft-1",
            start_time="2026-10-31T20:05:00+05:30",
            end_time="2026-10-31T14:35:00Z",
        )
        with (
            patch.object(
                calendar_routes,
                "_resolve_user_account",
                return_value=(SimpleNamespace(id="user-1"), "token"),
            ),
            patch.object(calendar_routes, "_get_bearer_token", return_value="token"),
        ):
            with self.assertRaises(HTTPException) as error:
                calendar_routes.confirm_calendar_event(
                    request,
                    authorization="Bearer token",
                    db=FakeDatabase(),
                )

        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
