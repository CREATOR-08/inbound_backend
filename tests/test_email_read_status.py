import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from app.api.routes import emails
from app.db.models import Email, EmailAgentStateModel, UserAccount
from app.main import app
from app.services.gmail import GmailReauthorizationRequired, fetch_single_email, update_gmail_message_read_status


class FakeQuery:
    def __init__(self, items):
        self._items = list(items)

    def options(self, *args, **kwargs):
        return self

    def filter(self, *conditions):
        filtered = []
        for item in self._items:
            match = True
            for cond in conditions:
                # Basic representation of SQLAlchemy binary expressions for tests
                # If condition checks Email.user_id == user.id
                left = getattr(cond, "left", None)
                right = getattr(cond, "right", None)
                operator_name = getattr(getattr(cond, "operator", None), "__name__", "")
                if left is not None and right is not None:
                    col_name = getattr(left, "key", getattr(left, "name", None))
                    val = getattr(right, "value", right)
                    if hasattr(item, col_name):
                        item_val = getattr(item, col_name)
                        if operator_name == "eq" and item_val != val:
                            match = False
                            break
            if match:
                filtered.append(item)
        return FakeQuery(filtered)

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class FakeDatabaseSession:
    def __init__(self, emails_list=None):
        self.emails = emails_list or []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def query(self, model, *args):
        if model is Email:
            return FakeQuery(self.emails)
        return FakeQuery([])

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class EmailReadStatusTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            refresh_token="refresh-token",
            token_expiry=None,
        )

    def test_fetch_single_email_sets_is_read_based_on_labels(self):
        fake_message_unread = {
            "id": "msg-unread",
            "threadId": "t1",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "test@example.com"},
                    {"name": "Subject", "value": "Unread Subject"},
                    {"name": "Date", "value": "Thu, 31 Oct 2026 12:00:00 +0000"},
                ],
                "body": {"data": ""},
            },
            "snippet": "Snippet unread",
        }
        fake_message_read = {
            "id": "msg-read",
            "threadId": "t2",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "test@example.com"},
                    {"name": "Subject", "value": "Read Subject"},
                    {"name": "Date", "value": "Thu, 31 Oct 2026 12:00:00 +0000"},
                ],
                "body": {"data": ""},
            },
            "snippet": "Snippet read",
        }

        mock_service = MagicMock()
        mock_messages = mock_service.users().messages()
        mock_messages.get.return_value.execute.side_effect = [
            fake_message_unread,
            fake_message_read,
        ]

        with patch("app.services.gmail.get_gmail_service_from_token", return_value=(mock_service, "token")):
            result_unread = fetch_single_email("token", "msg-unread")
            result_read = fetch_single_email("token", "msg-read")

        self.assertFalse(result_unread["is_read"])
        self.assertTrue(result_read["is_read"])

    def test_fetch_single_email_uses_configured_google_api_retries(self):
        message = {
            "id": "message-1",
            "payload": {"headers": [], "body": {"data": ""}},
            "snippet": "Snippet",
        }
        mock_service = MagicMock()
        mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = message

        with patch(
            "app.services.gmail.get_gmail_service_from_token",
            return_value=(mock_service, "token"),
        ):
            fetch_single_email(
                "token",
                "message-1",
                num_retries=2,
            )

        mock_service.users.return_value.messages.return_value.get.return_value.execute.assert_called_once_with(
            num_retries=2
        )

    def test_update_gmail_message_read_status_calls_modify(self):
        mock_service = MagicMock()
        mock_messages = mock_service.users().messages()

        with patch("app.services.gmail.get_gmail_service_from_token", return_value=(mock_service, "token")):
            update_gmail_message_read_status("token", "msg-1", is_read=True)
            mock_messages.modify.assert_called_with(
                userId="me",
                id="msg-1",
                body={"removeLabelIds": ["UNREAD"]},
            )

            update_gmail_message_read_status("token", "msg-2", is_read=False)
            mock_messages.modify.assert_called_with(
                userId="me",
                id="msg-2",
                body={"addLabelIds": ["UNREAD"]},
            )

    def test_get_emails_includes_is_read_and_filters_correctly(self):
        e1 = SimpleNamespace(
            id="e1",
            thread_id="t1",
            user_id="user-1",
            sender="a@test.com",
            subject="Sub 1",
            preview="prev 1",
            created_at=datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc),
            category="INFORMATIONAL",
            priority="LOW",
            is_archived=False,
            is_read=True,
            state=None,
        )
        e2 = SimpleNamespace(
            id="e2",
            thread_id="t2",
            user_id="user-1",
            sender="b@test.com",
            subject="Sub 2",
            preview="prev 2",
            created_at=datetime(2026, 10, 31, 11, 0, tzinfo=timezone.utc),
            category="ACTION_REQUIRED",
            priority="HIGH",
            is_archived=False,
            is_read=False,
            state=None,
        )

        db = FakeDatabaseSession([e1, e2])

        def override_get_db():
            yield db

        app.dependency_overrides[emails.get_db] = override_get_db
        try:
            with (
                patch.object(emails, "_resolve_user_account", return_value=(self.user, "token")),
                TestClient(app) as client,
            ):
                # 1. Fetch all
                res_all = client.get("/api/v1/emails", headers={"Authorization": "Bearer token"})
                self.assertEqual(res_all.status_code, 200)
                data_all = res_all.json()["data"]
                self.assertEqual(len(data_all), 2)
                self.assertTrue(data_all[0]["is_read"])
                self.assertFalse(data_all[1]["is_read"])

                # 2. Filter is_read=true
                res_read = client.get("/api/v1/emails?is_read=true", headers={"Authorization": "Bearer token"})
                self.assertEqual(res_read.status_code, 200)
                data_read = res_read.json()["data"]
                self.assertEqual(len(data_read), 1)
                self.assertEqual(data_read[0]["id"], "e1")
                self.assertTrue(data_read[0]["is_read"])

                # 3. Filter is_read=false
                res_unread = client.get("/api/v1/emails?is_read=false", headers={"Authorization": "Bearer token"})
                self.assertEqual(res_unread.status_code, 200)
                data_unread = res_unread.json()["data"]
                self.assertEqual(len(data_unread), 1)
                self.assertEqual(data_unread[0]["id"], "e2")
                self.assertFalse(data_unread[0]["is_read"])

                # 4. Filter unread=true
                res_unread2 = client.get("/api/v1/emails?unread=true", headers={"Authorization": "Bearer token"})
                self.assertEqual(res_unread2.status_code, 200)
                self.assertEqual(len(res_unread2.json()["data"]), 1)
                self.assertEqual(res_unread2.json()["data"][0]["id"], "e2")

                # 5. Filter status=read
                res_status_read = client.get("/api/v1/emails?status=read", headers={"Authorization": "Bearer token"})
                self.assertEqual(res_status_read.status_code, 200)
                self.assertEqual(len(res_status_read.json()["data"]), 1)
                self.assertEqual(res_status_read.json()["data"][0]["id"], "e1")
        finally:
            app.dependency_overrides.pop(emails.get_db, None)

    def test_open_email_marks_is_read_true_and_syncs_gmail(self):
        email_obj = SimpleNamespace(
            id="email-unread",
            thread_id="t1",
            user_id="user-1",
            sender="a@test.com",
            subject="Sub",
            preview="prev",
            body="Plain body",
            body_html="<p>Html body</p>",
            created_at=datetime.now(timezone.utc),
            category="INFORMATIONAL",
            priority="LOW",
            is_archived=False,
            is_read=False,
            state=None,
        )
        db = FakeDatabaseSession([email_obj])

        def override_get_db():
            yield db

        app.dependency_overrides[emails.get_db] = override_get_db
        try:
            with (
                patch.object(emails, "_resolve_user_account", return_value=(self.user, "token")),
                patch.object(emails, "update_gmail_message_read_status") as mock_gmail_sync,
                TestClient(app) as client,
            ):
                res = client.get(
                    "/api/v1/emails/email-unread/body",
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["body"], "Plain body")
                self.assertTrue(email_obj.is_read)
                self.assertTrue(db.committed)
                mock_gmail_sync.assert_called_once()
                call_kwargs = mock_gmail_sync.call_args.kwargs
                self.assertEqual(call_kwargs["access_token"], "token")
                self.assertEqual(call_kwargs["message_id"], "email-unread")
                self.assertTrue(call_kwargs["is_read"])

                # Second request when already read does not re-call Gmail sync
                mock_gmail_sync.reset_mock()
                res2 = client.get(
                    "/api/v1/emails/email-unread/body",
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res2.status_code, 200)
                mock_gmail_sync.assert_not_called()
        finally:
            app.dependency_overrides.pop(emails.get_db, None)

    def test_mark_email_unread_updates_database_and_syncs_gmail(self):
        email_obj = SimpleNamespace(
            id="email-read",
            thread_id="t1",
            user_id="user-1",
            sender="a@test.com",
            subject="Sub",
            preview="prev",
            body="Plain body",
            body_html="",
            created_at=datetime.now(timezone.utc),
            category="INFORMATIONAL",
            priority="LOW",
            is_archived=False,
            is_read=True,
            state=None,
        )
        db = FakeDatabaseSession([email_obj])

        def override_get_db():
            yield db

        app.dependency_overrides[emails.get_db] = override_get_db
        try:
            with (
                patch.object(emails, "_resolve_user_account", return_value=(self.user, "token")),
                patch.object(emails, "update_gmail_message_read_status") as mock_gmail_sync,
                TestClient(app) as client,
            ):
                # 1. Test POST /api/v1/emails/{id}/unread
                res = client.post(
                    "/api/v1/emails/email-read/unread",
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["status"], "success")
                self.assertFalse(res.json()["is_read"])
                self.assertFalse(email_obj.is_read)
                mock_gmail_sync.assert_called_with(
                    access_token="token",
                    message_id="email-read",
                    is_read=False,
                    refresh_token="refresh-token",
                    token_expiry=None,
                    on_token_refresh=unittest.mock.ANY,
                )

                # 2. Test PATCH /api/v1/emails/{id} with is_read=False
                email_obj.is_read = True
                res_patch = client.patch(
                    "/api/v1/emails/email-read",
                    json={"is_read": False},
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res_patch.status_code, 200)
                self.assertFalse(email_obj.is_read)

                # 3. Test POST /api/v1/actions/unread
                email_obj.is_read = True
                res_action = client.post(
                    "/api/v1/actions/unread",
                    json={"email_id": "email-read"},
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res_action.status_code, 200)
                self.assertFalse(email_obj.is_read)

                # 4. Test POST /api/v1/emails/{id}/read
                res_read = client.post(
                    "/api/v1/emails/email-read/read",
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res_read.status_code, 200)
                self.assertTrue(email_obj.is_read)

                # 5. Test POST /api/v1/actions/read
                email_obj.is_read = False
                res_action_read = client.post(
                    "/api/v1/actions/read",
                    json={"email_id": "email-read"},
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res_action_read.status_code, 200)
                self.assertTrue(email_obj.is_read)
        finally:
            app.dependency_overrides.pop(emails.get_db, None)

    def test_mark_email_unread_reverts_db_when_gmail_sync_fails(self):
        email_obj = SimpleNamespace(
            id="email-fail",
            thread_id="t1",
            user_id="user-1",
            sender="a@test.com",
            subject="Sub",
            preview="prev",
            body="Plain body",
            body_html="",
            created_at=datetime.now(timezone.utc),
            category="INFORMATIONAL",
            priority="LOW",
            is_archived=False,
            is_read=True,
            state=None,
        )
        db = FakeDatabaseSession([email_obj])

        def override_get_db():
            yield db

        app.dependency_overrides[emails.get_db] = override_get_db
        try:
            with (
                patch.object(emails, "_resolve_user_account", return_value=(self.user, "token")),
                patch.object(
                    emails,
                    "update_gmail_message_read_status",
                    side_effect=RuntimeError("Gmail API network failure"),
                ),
                TestClient(app) as client,
            ):
                res = client.post(
                    "/api/v1/emails/email-fail/unread",
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(res.status_code, 502)
                # Ensure the is_read property was reverted back to True
                self.assertTrue(email_obj.is_read)
        finally:
            app.dependency_overrides.pop(emails.get_db, None)


if __name__ == "__main__":
    unittest.main()
