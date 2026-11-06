import unittest

from app.services.calendar_extraction import resolve_calendar_event_draft


def extract(date_phrase, time_phrase="7 PM", **fields):
    source = f"Let's meet {date_phrase} at {time_phrase}"
    if fields.get("end_time"):
        source += f" to {fields['end_time']}"
    duration = fields.get("duration", "1 hour")
    if duration:
        source += f" for {duration}"
    if fields.get("timezone"):
        source += f" {fields['timezone']}"
    return resolve_calendar_event_draft(
        {
            "title": "Planning",
            "date_expression": date_phrase,
            "start_time": time_phrase,
            "end_time": fields.get("end_time", ""),
            "duration": duration,
            "timezone": fields.get("timezone", ""),
            "confidence": 0.9,
            "attendees": [],
        },
        source_text=source,
        received_at=fields.get("received_at", "2026-11-06T09:00:00Z"),
    )


class CalendarExtractionTests(unittest.TestCase):
    def test_tomorrow_and_yesterday_use_received_at_in_ist(self):
        tomorrow = extract("tomorrow")
        yesterday = extract("yesterday")
        self.assertEqual(tomorrow["start_time"], "2026-11-07T19:00:00+05:30")
        self.assertEqual(yesterday["start_time"], "2026-11-05T19:00:00+05:30")

    def test_today_and_in_two_or_three_days_are_supported(self):
        self.assertEqual(extract("today")["start_time"], "2026-11-06T19:00:00+05:30")
        self.assertEqual(extract("in two days")["start_time"], "2026-11-08T19:00:00+05:30")
        self.assertEqual(extract("in three days")["start_time"], "2026-11-09T19:00:00+05:30")

    def test_day_after_and_before_yesterday_are_resolved(self):
        self.assertEqual(
            extract("day after tomorrow")["start_time"],
            "2026-11-08T19:00:00+05:30",
        )
        self.assertEqual(
            extract("day before yesterday")["start_time"],
            "2026-11-04T19:00:00+05:30",
        )

    def test_next_and_this_weekday_are_resolved_from_ist_anchor(self):
        self.assertEqual(
            extract("next Monday")["start_time"],
            "2026-11-09T19:00:00+05:30",
        )
        self.assertEqual(
            extract("this Friday")["start_time"],
            "2026-11-06T19:00:00+05:30",
        )

    def test_relative_date_rolls_over_year_boundary(self):
        result = extract(
            "tomorrow",
            received_at="2026-12-31T20:00:00Z",
        )
        self.assertEqual(result["start_time"], "2027-01-02T19:00:00+05:30")

    def test_missing_ampm_and_duration_returns_actionable_clarification(self):
        result = resolve_calendar_event_draft(
            {
                "date_expression": "Tomorrow",
                "start_time": "7",
                "duration": "",
                "confidence": 0.9,
            },
            source_text="Hello meeting Tomorrow at 7",
            received_at="2026-11-06T09:00:00Z",
        )
        self.assertEqual(result["extraction_status"], "ambiguous")
        self.assertEqual(
            result["ambiguity"],
            "Did you mean 7 AM or 7 PM? What duration should I use?",
        )
        self.assertEqual(result["start_time"], "")
        self.assertEqual(result["end_time"], "")

    def test_missing_duration_is_not_invented(self):
        result = extract("tomorrow", "7 PM", duration="")
        self.assertEqual(result["extraction_status"], "missing")
        self.assertEqual(result["ambiguity"], "What duration should I use?")
        self.assertEqual(result["end_time"], "")

    def test_explicit_ampm_and_duration_produce_complete_event(self):
        result = extract("tomorrow", "7 PM", duration="45 minutes")
        self.assertEqual(result["extraction_status"], "complete")
        self.assertEqual(result["start_time"], "2026-11-07T19:00:00+05:30")
        self.assertEqual(result["end_time"], "2026-11-07T19:45:00+05:30")

    def test_explicit_ampm_in_email_overrides_model_omission(self):
        result = resolve_calendar_event_draft(
            {
                "date_expression": "tomorrow",
                "start_time": "7",
                "duration": "45 minutes",
                "confidence": 0.9,
            },
            source_text="Let's meet tomorrow at 7 PM for 45 minutes",
            received_at="2026-11-06T09:00:00Z",
        )
        self.assertEqual(result["extraction_status"], "complete")
        self.assertEqual(result["start_time"], "2026-11-07T19:00:00+05:30")

    def test_explicit_timezone_in_email_is_used_if_model_omits_it(self):
        result = resolve_calendar_event_draft(
            {
                "date_expression": "tomorrow",
                "start_time": "7 PM",
                "duration": "1 hour",
                "timezone": "",
                "confidence": 0.9,
            },
            source_text="Let's meet tomorrow at 7 PM UTC for 1 hour",
            received_at="2026-11-06T09:00:00Z",
        )
        self.assertEqual(result["start_time"], "2026-11-07T19:00:00+00:00")

    def test_explicit_duration_is_detected_from_email_even_if_model_omits_it(self):
        result = resolve_calendar_event_draft(
            {
                "date_expression": "tomorrow",
                "start_time": "7 PM",
                "duration": "",
                "confidence": 0.9,
            },
            source_text="Let's meet tomorrow at 7 PM for 45 minutes",
            received_at="2026-11-06T09:00:00Z",
        )
        self.assertEqual(result["extraction_status"], "complete")
        self.assertEqual(result["end_time"], "2026-11-07T19:45:00+05:30")

    def test_explicit_timezone_preserves_the_wall_time_and_instant(self):
        result = extract("tomorrow", "7 PM", duration="1 hour", timezone="UTC")
        self.assertEqual(result["start_time"], "2026-11-07T19:00:00+00:00")
        self.assertEqual(result["end_time"], "2026-11-07T20:00:00+00:00")

    def test_dst_gap_and_repeated_local_times_require_clarification(self):
        for date_phrase, clock_phrase, clarification in (
            (
                "March 8, 2026",
                "2:30 AM",
                "does not exist",
            ),
            (
                "November 1, 2026",
                "1:30 AM",
                "occurs twice",
            ),
        ):
            with self.subTest(date=date_phrase, clock=clock_phrase):
                result = resolve_calendar_event_draft(
                    {
                        "date_expression": date_phrase,
                        "start_time": clock_phrase,
                        "duration": "30 minutes",
                        "timezone": "America/New_York",
                        "confidence": 0.9,
                    },
                    source_text=(
                        f"Let's meet {date_phrase} at {clock_phrase} "
                        f"for 30 minutes in America/New_York"
                    ),
                    received_at="2026-01-01T09:00:00Z",
                )
                self.assertEqual(result["extraction_status"], "ambiguous")
                self.assertIn(clarification, result["ambiguity"])

    def test_specific_month_date_uses_anchor_year_without_guessing_past_year(self):
        result = extract(
            "December 12",
            received_at="2026-11-06T09:00:00Z",
            duration="1 hour",
        )
        self.assertEqual(result["start_time"], "2026-12-12T19:00:00+05:30")

        ambiguous = extract(
            "December 12",
            received_at="2026-12-20T09:00:00Z",
            duration="1 hour",
        )
        self.assertEqual(ambiguous["extraction_status"], "ambiguous")
        self.assertIn("Which year", ambiguous["ambiguity"])

    def test_day_month_specific_date_is_supported(self):
        result = extract("12 December", duration="30 minutes")
        self.assertEqual(result["start_time"], "2026-12-12T19:00:00+05:30")

    def test_explicit_end_time_does_not_require_a_duration(self):
        result = extract(
            "tomorrow",
            "7 PM",
            end_time="8 PM",
            duration="",
        )
        self.assertEqual(result["extraction_status"], "complete")
        self.assertEqual(result["end_time"], "2026-11-07T20:00:00+05:30")

    def test_next_week_without_a_weekday_is_ambiguous(self):
        result = extract("next week")
        self.assertEqual(result["extraction_status"], "ambiguous")
        self.assertEqual(result["ambiguity"], "Which day next week should I schedule the meeting?")

    def test_invalid_or_unverified_date_is_not_used(self):
        result = resolve_calendar_event_draft(
            {
                "date_expression": "tomorrow",
                "start_time": "7 PM",
                "duration": "1 hour",
                "confidence": 0.9,
            },
            source_text="Let's meet Friday at 7 PM for 1 hour",
            received_at="2026-11-06T09:00:00Z",
        )
        self.assertEqual(result["extraction_status"], "invalid")
        self.assertEqual(result["start_time"], "")

    def test_missing_email_timestamp_does_not_use_machine_date(self):
        result = extract("tomorrow", received_at=None)
        self.assertEqual(result["extraction_status"], "invalid")
        self.assertIn("received timestamp is unavailable", result["ambiguity"])


if __name__ == "__main__":
    unittest.main()
