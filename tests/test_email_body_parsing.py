import base64
import unittest

from app.services.gmail import (
    _decode_base64_payload,
    _extract_bodies_from_payload,
    clean_text_for_llm,
    fetch_single_email,
)


def encoded(value):
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class EmailBodyParsingTests(unittest.TestCase):
    def test_decodes_urlsafe_base64_as_utf8(self):
        body = "Meeting tomorrow at 7 PM — café"
        self.assertEqual(_decode_base64_payload(encoded(body)), body)

    def test_plain_text_part_is_selected_for_multipart_alternative(self):
        plain = "Let's meet tomorrow at 7 PM."
        html = "<p>Let's meet <b>tomorrow</b> at 7 PM.</p>"
        body, body_html = _extract_bodies_from_payload(
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encoded(plain)}},
                    {"mimeType": "text/html", "body": {"data": encoded(html)}},
                ],
            }
        )
        self.assertEqual(body, plain)
        self.assertEqual(body_html, html)

    def test_html_only_body_becomes_readable_text_with_entities_and_lines(self):
        body, body_html = _extract_bodies_from_payload(
            {
                "mimeType": "text/html",
                "body": {
                    "data": encoded(
                        "<html><head><title>ignored</title></head><body>"
                        "<p>Meeting &amp; review</p><p>Tomorrow at <b>7 PM</b>.</p>"
                        "<script>not meeting information</script></body></html>"
                    )
                },
            }
        )
        self.assertEqual(body, "Meeting & review\nTomorrow at 7 PM.")
        self.assertIn("<p>Meeting", body_html)

    def test_ignores_attachment_text_and_preserves_body_parts(self):
        body, _ = _extract_bodies_from_payload(
            {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": encoded("Meet tomorrow at 7 PM.")},
                    },
                    {
                        "mimeType": "text/plain",
                        "filename": "old-thread.txt",
                        "body": {"data": encoded("old unrelated thread")},
                    },
                ],
            }
        )
        self.assertEqual(body, "Meet tomorrow at 7 PM.")

    def test_invalid_base64_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "malformed encoded message body"):
            _decode_base64_payload("not-base64?")

    def test_quoted_replies_are_trimmed_only_after_current_meeting_details(self):
        text = (
            "Let's meet tomorrow at 7 PM.\n\n"
            "On Friday, someone wrote:\n"
            "A long unrelated quoted message."
        )
        self.assertEqual(
            clean_text_for_llm(text),
            "Let's meet tomorrow at 7 PM.",
        )
        quoted_contains_details = "Sounds good.\nOn Friday, someone wrote:\nMeet tomorrow at 7."
        self.assertEqual(clean_text_for_llm(quoted_contains_details), quoted_contains_details)

    def test_scheduler_body_parser_does_not_replace_empty_body_with_snippet(self):
        message = {
            "id": "message-1",
            "internalDate": "1793955600000",
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": ""},
                "headers": [],
            },
            "snippet": "Preview only",
        }

        class Request:
            def execute(self):
                return message

        class Service:
            def users(self):
                return self

            def messages(self):
                return self

            def get(self, **_kwargs):
                return Request()

        from unittest.mock import patch

        with patch(
            "app.services.gmail.get_gmail_service_from_token",
            return_value=(Service(), "token"),
        ):
            result = fetch_single_email("token", "message-1")

        self.assertEqual(result["body"], "")
        self.assertEqual(result["preview"], "Preview only")
        self.assertEqual(result["received_at"].isoformat(), "2026-11-06T09:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
