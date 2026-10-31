import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, Response
from fastapi.testclient import TestClient

from app.api.routes import emails
from app.api.schemas.schemas import PubSubPushRequest


class FakeQuery:
    def __init__(self, database):
        self.database = database

    def filter(self, *conditions):
        self.database.filters.append(conditions)
        return self

    def first(self):
        return self.database.user

    def one(self):
        return self.database.user

    def update(self, values, synchronize_session=False):
        self.database.synchronize_session = synchronize_session
        user = self.database.user
        if user.initial_sync_completed_at or user.initial_sync_status not in (
            None,
            "failed",
        ):
            return 0
        user.initial_sync_status = values[emails.UserAccount.initial_sync_status]
        return 1


class FakeDatabase:
    def __init__(self, user):
        self.user = user
        self.closed = False
        self.commits = 0
        self.filters = []

    def query(self, *_models):
        self.query_models = _models
        return FakeQuery(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def refresh(self, _user):
        self.refreshed_user = _user


def _sync_user(status=None, completed_at=None):
    return SimpleNamespace(
        id="user-1",
        email="user@example.com",
        access_token="access-token",
        refresh_token="refresh-token",
        token_expiry=None,
        initial_sync_status=status,
        initial_sync_completed_at=completed_at,
        last_history_id="123",
        watch_expiration="9999999999999",
    )


def _pubsub_request(payload: dict) -> PubSubPushRequest:
    data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return PubSubPushRequest(message={"data": data})


class InitialEmailSyncEndpointTests(unittest.TestCase):
    def test_sync_initial_does_not_require_background_tasks_query_parameter(self):
        from app.main import app

        user = SimpleNamespace(
            id="user-1",
            refresh_token="refresh-token",
            token_expiry=None,
            initial_sync_completed_at=True,
            last_history_id="12345",
        )

        class FakeDatabase:
            def commit(self):
                pass

            def close(self):
                pass

        def override_get_db():
            yield FakeDatabase()

        app.dependency_overrides[emails.get_db] = override_get_db
        try:
            with (
                patch.object(
                    emails,
                    "_resolve_user_account",
                    return_value=(user, "access-token"),
                ),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/api/v1/emails/sync-initial",
                    headers={"Authorization": "Bearer access-token"},
                )
        finally:
            app.dependency_overrides.pop(emails.get_db, None)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "already_completed")

    def test_sync_initial_returns_promptly_and_registers_one_task(self):
        user = _sync_user()
        request_db = FakeDatabase(user)
        background_tasks = BackgroundTasks()
        response = Response()

        with patch.object(
            emails,
            "_resolve_user_account",
            return_value=(user, "access-token"),
        ), patch.object(emails, "run_initial_gmail_sync") as worker:
            started = time.perf_counter()
            result = emails.sync_initial_emails(
                background_tasks,
                response,
                "Bearer access-token",
                request_db,
            )
            response_ms = (time.perf_counter() - started) * 1000

        self.assertEqual(response.status_code, 202)
        self.assertEqual(result.status, "pending")
        self.assertLess(response_ms, 100)
        self.assertEqual(len(background_tasks.tasks), 1)
        self.assertIs(background_tasks.tasks[0].func, worker)
        self.assertEqual(background_tasks.tasks[0].args, ("user-1",))
        worker.assert_not_called()
        self.assertTrue(request_db.closed)

    def test_http_sync_initial_returns_202_and_runs_registered_task(self):
        from app.main import app

        user = _sync_user()
        request_db = FakeDatabase(user)

        def override_get_db():
            yield request_db

        app.dependency_overrides[emails.get_db] = override_get_db
        try:
            with (
                patch.object(
                    emails,
                    "_resolve_user_account",
                    return_value=(user, "access-token"),
                ),
                patch.object(emails, "run_initial_gmail_sync") as worker,
                TestClient(app) as client,
            ):
                response = client.post(
                    "/api/v1/emails/sync-initial",
                    headers={"Authorization": "Bearer access-token"},
                )
        finally:
            app.dependency_overrides.pop(emails.get_db, None)

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "pending")
        worker.assert_called_once_with("user-1")

    def test_duplicate_sync_request_does_not_register_another_task(self):
        user = _sync_user()
        request_db = FakeDatabase(user)
        background_tasks = BackgroundTasks()
        response = Response()

        with patch.object(
            emails,
            "_resolve_user_account",
            return_value=(user, "access-token"),
        ):
            first = emails.sync_initial_emails(
                background_tasks,
                response,
                "Bearer access-token",
                request_db,
            )
            second = emails.sync_initial_emails(
                background_tasks,
                Response(),
                "Bearer access-token",
                request_db,
            )

        self.assertEqual(first.status, "pending")
        self.assertEqual(second.status, "pending")
        self.assertEqual(len(background_tasks.tasks), 1)

    def test_worker_syncs_using_independent_sessions_and_marks_completed(self):
        user = _sync_user(status="pending")
        worker_databases = []

        def create_worker_database():
            database = FakeDatabase(user)
            worker_databases.append(database)
            return database

        message = {
            "id": "message-1",
            "thread_id": "thread-1",
            "sender": "sender@example.com",
            "subject": "Subject",
            "preview": "Preview",
            "body": "Body",
            "body_html": "",
            "has_html": False,
            "received_at": None,
        }
        with (
            patch.object(emails, "SessionLocal", side_effect=create_worker_database),
            patch.object(emails, "list_message_ids", return_value=(["message-1"], "access-token")),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                return_value=([], "456", "access-token"),
            ),
            patch.object(emails, "fetch_single_email", return_value=message),
            patch.object(emails, "_store_email_if_new", return_value=True),
            patch.object(emails, "tag_recent_untagged_emails") as tag_emails,
        ):
            started = time.perf_counter()
            emails.run_initial_gmail_sync(user.id)
            worker_ms = (time.perf_counter() - started) * 1000

        self.assertGreaterEqual(worker_ms, 0)
        self.assertEqual(user.initial_sync_status, "completed")
        self.assertIsNotNone(user.initial_sync_completed_at)
        self.assertEqual(user.last_history_id, "456")
        self.assertEqual(len(worker_databases), 3)
        self.assertTrue(all(database.closed for database in worker_databases))
        tag_emails.assert_called_once_with("user-1")

    def test_worker_failure_is_recorded_and_propagated(self):
        user = _sync_user(status="pending")
        worker_databases = []

        def create_worker_database():
            database = FakeDatabase(user)
            worker_databases.append(database)
            return database

        with (
            patch.object(emails, "SessionLocal", side_effect=create_worker_database),
            patch.object(
                emails,
                "list_message_ids",
                side_effect=RuntimeError("Gmail API unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "Gmail API unavailable"),
        ):
            emails.run_initial_gmail_sync(user.id)

        self.assertEqual(user.initial_sync_status, "failed")
        self.assertTrue(all(database.closed for database in worker_databases))

    def test_sync_status_endpoint_reports_current_user_status(self):
        user = _sync_user(status="running")
        request_db = FakeDatabase(user)

        with patch.object(
            emails,
            "_resolve_user_account",
            return_value=(user, "access-token"),
        ):
            result = emails.get_initial_sync_status(
                "Bearer access-token",
                request_db,
            )

        self.assertEqual(result.status, "running")
        self.assertEqual(result.history_id, "123")


class GmailPubSubWebhookTests(unittest.TestCase):
    def test_gmail_sync_failure_propagates_without_acknowledgment(self):
        user = SimpleNamespace(
            id="user-1",
            access_token="access-token",
            refresh_token="refresh-token",
            token_expiry=None,
            last_history_id="123",
        )

        class FakeQuery:
            def filter(self, *_args):
                return self

            def first(self):
                return user

        class FakeDatabase:
            def query(self, *_args):
                return FakeQuery()

            def close(self):
                pass

        with (
            patch.object(emails, "SessionLocal", return_value=FakeDatabase()),
            patch.object(
                emails,
                "get_gmail_service_with_token_state",
                return_value=(None, "access-token", None),
            ),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                side_effect=RuntimeError("Gmail API unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "Gmail API unavailable"),
        ):
            emails.process_realtime_email_update("user@example.com", "456")

    def test_acknowledges_after_successful_processing(self):
        request = _pubsub_request(
            {"emailAddress": "user@example.com", "historyId": "12345"}
        )

        with patch.object(emails, "process_realtime_email_update") as process:
            response = emails.gmail_pubsub_webhook(request)

        process.assert_called_once_with("user@example.com", "12345")
        self.assertEqual(response.status_code, 204)

    def test_returns_retryable_error_when_processing_fails(self):
        request = _pubsub_request(
            {"emailAddress": "user@example.com", "historyId": "12345"}
        )

        with (
            patch.object(
                emails,
                "process_realtime_email_update",
                side_effect=RuntimeError("Gmail API unavailable"),
            ),
            self.assertRaises(HTTPException) as error,
        ):
            emails.gmail_pubsub_webhook(request)

        self.assertEqual(error.exception.status_code, 503)

    def test_rejects_malformed_notification(self):
        request = PubSubPushRequest(message={"data": "not-base64"})

        with self.assertRaises(HTTPException) as error:
            emails.gmail_pubsub_webhook(request)

        self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
