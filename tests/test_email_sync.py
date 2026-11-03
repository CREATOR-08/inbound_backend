import base64
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from types import SimpleNamespace
from unittest.mock import ANY, patch

from fastapi import BackgroundTasks, HTTPException, Response
from fastapi.testclient import TestClient

from app.api.routes import emails
from app.api.schemas.schemas import PubSubPushRequest
from app.services.gmail import GmailHistoryCursorExpired


class FakeQuery:
    def __init__(self, database):
        self.database = database
        self.model = None

    def filter(self, *conditions):
        self.database.filters.append(conditions)
        return self

    def with_for_update(self):
        self.database.locked = True
        return self

    def first(self):
        return self.database.user

    def one(self):
        return self.database.user

    def scalar(self):
        if self.model is emails.UserAccount.incremental_sync_status:
            return self.database.user.incremental_sync_status
        return self.database.email_count

    def options(self, *_options):
        return self

    def order_by(self, *_ordering):
        return self

    def all(self):
        return self.database.emails

    def update(self, values, synchronize_session=False):
        self.database.synchronize_session = synchronize_session
        with self.database.update_lock:
            user = self.database.user
            if emails.UserAccount.initial_sync_status in values:
                status_column = emails.UserAccount.initial_sync_status
                new_status = values[status_column]
                expected_status = (
                    "not_started" if new_status == "pending" else "pending"
                )
                if (
                    not user.initial_sync_eligible
                    or user.initial_sync_status != expected_status
                ):
                    return 0
                user.initial_sync_status = new_status
                user.updated_at = datetime.now(timezone.utc)
                return 1
            if emails.UserAccount.updated_at in values:
                stale_before = datetime.now(timezone.utc) - timedelta(
                    seconds=emails.INITIAL_SYNC_RECOVERY_TIMEOUT_SECONDS
                )
                if (
                    not user.initial_sync_eligible
                    or user.initial_sync_status != "pending"
                    or user.updated_at is None
                    or user.updated_at >= stale_before
                ):
                    return 0
                user.updated_at = datetime.now(timezone.utc)
                return 1
            return 0


class FakeDatabase:
    def __init__(self, user, email_count=0, emails=None):
        self.user = user
        self.closed = False
        self.commits = 0
        self.filters = []
        self.email_count = email_count
        self.emails = emails or []
        self.locked = False
        self.update_lock = Lock()

    def query(self, *models):
        query = FakeQuery(self)
        query.model = models[0] if models else None
        self.query_models = models
        return query

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def refresh(self, _user):
        self.refreshed_user = _user


def _sync_user(status="not_started", completed_at=None):
    return SimpleNamespace(
        id="user-1",
        email="user@example.com",
        access_token="access-token",
        refresh_token="refresh-token",
        token_expiry=None,
        initial_sync_status=status,
        initial_sync_completed_at=completed_at,
        initial_sync_eligible=status == "not_started",
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        updated_at=datetime.now(timezone.utc),
        last_history_id="123",
        watch_expiration="9999999999999",
        incremental_sync_status=None,
        incremental_sync_id=None,
    )


def _pubsub_request(payload: dict) -> PubSubPushRequest:
    data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return PubSubPushRequest(message={"data": data})


class InitialEmailSyncEndpointTests(unittest.TestCase):
    def setUp(self):
        auth_token_patch = patch.object(
            emails,
            "_get_bearer_token",
            return_value="access-token",
        )
        auth_token_patch.start()
        self.addCleanup(auth_token_patch.stop)

    def test_sync_initial_does_not_require_background_tasks_query_parameter(self):
        from app.main import app

        user = _sync_user(status="completed", completed_at=True)
        request_db = FakeDatabase(user)
        status_db = FakeDatabase(user)

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
                patch.object(
                    emails,
                    "_register_user_gmail_watch",
                    return_value={"historyId": "123", "expiration": "9999999999999"},
                ),
                patch.object(
                    emails,
                    "process_realtime_email_update",
                    side_effect=lambda *_args, **_kwargs: setattr(
                        user, "incremental_sync_status", "completed"
                    ),
                ),
                patch.object(emails, "SessionLocal", return_value=status_db),
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
        self.assertEqual(response.json()["sync_mode"], "incremental")

    def test_sync_initial_returns_promptly_and_registers_one_task(self):
        user = _sync_user(status="not_started")
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
        self.assertEqual(result.account_status, "new")
        self.assertEqual(result.sync_mode, "initial")
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

    def test_stale_pending_initial_sync_is_recovered(self):
        user = _sync_user(status="pending")
        user.initial_sync_eligible = True
        user.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        database = FakeDatabase(user)
        tasks = BackgroundTasks()

        with patch.object(
            emails,
            "list_message_ids",
        ) as three_day_import:
            with patch.object(
                emails,
                "_resolve_user_account",
                return_value=(user, "access-token"),
            ):
                response = Response()
                result = emails.sync_initial_emails(
                    tasks,
                    response,
                    "Bearer access-token",
                    database,
                )

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.account_status, "new")
        self.assertEqual(len(tasks.tasks), 1)
        self.assertIs(tasks.tasks[0].func, emails.run_initial_gmail_sync)
        self.assertGreater(
            user.updated_at,
            datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        three_day_import.assert_not_called()

    def test_concurrent_stale_recovery_claims_only_once(self):
        user = _sync_user(status="pending")
        user.initial_sync_eligible = True
        user.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        database = FakeDatabase(user)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(
                executor.map(
                    lambda _attempt: emails._claim_stale_initial_sync(
                        database,
                        user.id,
                    ),
                    range(2),
                )
            )

        self.assertEqual(sorted(claims), [0, 1])

    def test_completed_and_failed_initial_syncs_are_not_reclaimed(self):
        for status, completed_at, eligible in (
            ("completed", datetime.now(timezone.utc), False),
            ("failed", None, False),
        ):
            with self.subTest(status=status):
                user = _sync_user(status=status, completed_at=completed_at)
                user.initial_sync_eligible = eligible
                user.updated_at = datetime.now(timezone.utc) - timedelta(days=1)
                self.assertEqual(
                    emails._claim_stale_initial_sync(
                        FakeDatabase(user),
                        user.id,
                    ),
                    0,
                )
                self.assertEqual(user.initial_sync_status, status)
                self.assertFalse(user.initial_sync_eligible)

    def test_worker_syncs_using_independent_sessions_and_marks_completed(self):
        user = _sync_user(status="pending")
        user.initial_sync_eligible = True
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
        self.assertFalse(user.initial_sync_eligible)
        self.assertEqual(user.last_history_id, "456")
        self.assertEqual(len(worker_databases), 3)
        self.assertTrue(all(database.closed for database in worker_databases))
        tag_emails.assert_called_once_with("user-1")

    def test_worker_failure_is_recorded_and_propagated(self):
        user = _sync_user(status="pending")
        user.initial_sync_eligible = True
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
        self.assertFalse(user.initial_sync_eligible)
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
                BackgroundTasks(),
                "******",
                request_db,
            )

        self.assertEqual(result.status, "running")
        self.assertEqual(result.history_id, "123")

    def test_sync_status_poll_recovers_stale_pending_initial_sync(self):
        user = _sync_user(status="pending")
        user.initial_sync_eligible = True
        user.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        request_db = FakeDatabase(user)
        background_tasks = BackgroundTasks()

        with (
            patch.object(
                emails,
                "_resolve_user_account",
                return_value=(user, "access-token"),
            ),
            patch.object(emails, "run_initial_gmail_sync") as worker,
        ):
            result = emails.get_initial_sync_status(
                background_tasks,
                "******",
                request_db,
            )

        self.assertEqual(result.status, "pending")
        self.assertEqual(len(background_tasks.tasks), 1)
        self.assertIs(background_tasks.tasks[0].func, worker)
        self.assertEqual(background_tasks.tasks[0].args, ("user-1",))

    def test_existing_account_uses_incremental_sync_not_initial_import(self):
        user = _sync_user(status="pending")
        request_db = FakeDatabase(user, email_count=4)
        status_db = FakeDatabase(user, email_count=5)

        with (
            patch.object(
                emails,
                "_resolve_user_account",
                return_value=(user, "access-token"),
            ),
            patch.object(
                emails,
                "_register_user_gmail_watch",
                return_value={"historyId": "123", "expiration": "9999999999999"},
            ) as register_watch,
            patch.object(
                emails,
                "process_realtime_email_update",
                side_effect=lambda *_args, **_kwargs: setattr(
                    user, "incremental_sync_status", "completed"
                ),
            ) as incremental_sync,
            patch.object(emails, "SessionLocal", return_value=status_db),
            patch.object(emails, "run_initial_gmail_sync") as initial_sync,
            patch.object(emails, "list_message_ids") as three_day_list,
        ):
            result = emails.sync_initial_emails(
                BackgroundTasks(),
                Response(),
                "******",
                request_db,
            )

        self.assertEqual(result.account_status, "existing")
        self.assertEqual(result.sync_mode, "incremental")
        self.assertEqual(result.stored_email_count, 5)
        self.assertEqual(result.incremental_sync_status, "completed")
        register_watch.assert_called_once()
        incremental_sync.assert_called_once_with(
            "user@example.com",
            "123",
            True,
            ANY,
        )
        initial_sync.assert_not_called()
        three_day_list.assert_not_called()

    def test_existing_email_list_is_loaded_from_database(self):
        user = _sync_user(status=None)
        email = SimpleNamespace(
            id="email-1",
            thread_id="thread-1",
            sender="sender@example.com",
            subject="Stored subject",
            preview="Stored preview",
            created_at=None,
            category=None,
            priority=None,
            is_archived=False,
            is_read=False,
            state=None,
        )
        database = FakeDatabase(user, emails=[email])

        with patch.object(
            emails,
            "_resolve_user_account",
            return_value=(user, "access-token"),
        ):
            result = emails.get_emails(
                authorization="Bearer access-token",
                db=database,
            )

        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["data"][0]["id"], "email-1")
        self.assertEqual(result["data"][0]["subject"], "Stored subject")
        self.assertIn(
            str(emails.Email.user_id == user.id),
            [str(condition) for group in database.filters for condition in group],
        )

    def test_an_account_cannot_read_another_accounts_email(self):
        authenticated_user = _sync_user(status=None)
        other_email = SimpleNamespace(
            id="email-owned-by-user-2",
            user_id="user-2",
            is_read=False,
        )

        class IsolatedEmailQuery(FakeQuery):
            def first(self):
                return (
                    other_email
                    if authenticated_user.id == other_email.user_id
                    else None
                )

        class IsolatedEmailDatabase(FakeDatabase):
            def query(self, *models):
                query = IsolatedEmailQuery(self)
                query.model = models[0] if models else None
                return query

        database = IsolatedEmailDatabase(authenticated_user)
        with (
            patch.object(
                emails,
                "_get_bearer_token",
                return_value="access-token",
            ),
            patch.object(
                emails,
                "_resolve_user_account",
                return_value=(authenticated_user, "access-token"),
            ),
            self.assertRaises(HTTPException) as error,
        ):
            emails.get_email("email-owned-by-user-2", "******", database)

        self.assertEqual(error.exception.status_code, 404)

    def test_watch_renewal_preserves_saved_history_cursor(self):
        user = _sync_user(status=None)
        user.last_history_id = "123"
        user.watch_expiration = "1"

        with patch.object(
            emails,
            "start_gmail_watch",
            return_value={"historyId": "456", "expiration": "9999999999999"},
        ) as start_watch:
            result = emails._ensure_gmail_watch(user, "access-token")

        self.assertEqual(result["historyId"], "123")
        self.assertEqual(result["expiration"], "9999999999999")
        self.assertEqual(user.last_history_id, "123")
        start_watch.assert_called_once()


class GmailPubSubWebhookTests(unittest.TestCase):
    def test_expired_history_uses_full_inbox_recovery_not_three_day_import(self):
        user = _sync_user(status="completed")
        database = FakeDatabase(user)
        recovery_message = {
            "id": "message-1",
            "thread_id": "thread-1",
            "sender": "sender@example.com",
            "subject": "Recovered",
            "preview": "Preview",
            "body": "Body",
            "body_html": "",
            "has_html": False,
            "received_at": None,
        }

        with (
            patch.object(emails, "SessionLocal", return_value=database),
            patch.object(
                emails,
                "get_gmail_service_with_token_state",
                return_value=(None, "access-token", None),
            ),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                side_effect=[
                    GmailHistoryCursorExpired("cursor expired"),
                    (["message-1"], "900", "access-token"),
                ],
            ) as history,
            patch.object(
                emails,
                "start_gmail_watch",
                return_value={"historyId": "800", "expiration": "9999999999999"},
            ) as start_watch,
            patch.object(
                emails,
                "list_message_ids",
                return_value=(["message-1", "message-2"], "access-token"),
            ) as list_messages,
            patch.object(
                emails,
                "fetch_single_email",
                side_effect=lambda access_token, message_id, **_kwargs: {
                    **recovery_message,
                    "id": message_id,
                    "access_token": access_token,
                },
            ),
            patch.object(emails, "_upsert_recovered_email") as upsert,
            patch.object(emails, "tag_recent_untagged_emails"),
        ):
            emails.process_realtime_email_update("user@example.com", "124")

        self.assertEqual(user.last_history_id, "900")
        self.assertEqual(user.incremental_sync_status, "completed")
        self.assertEqual(user.watch_expiration, "9999999999999")
        self.assertEqual(history.call_count, 2)
        self.assertEqual(history.call_args.kwargs["start_history_id"], "800")
        self.assertEqual(
            history.call_args.kwargs["history_types"],
            ["messageAdded", "labelAdded", "labelRemoved"],
        )
        start_watch.assert_called_once()
        list_messages.assert_called_once()
        self.assertEqual(list_messages.call_args.kwargs["query"], "in:inbox")
        self.assertNotIn("newer_than:3d", list_messages.call_args.kwargs["query"])
        self.assertEqual(upsert.call_count, 2)

    def test_expired_history_recovery_failure_keeps_old_cursor(self):
        user = _sync_user(status="completed")
        database = FakeDatabase(user)

        with (
            patch.object(emails, "SessionLocal", return_value=database),
            patch.object(
                emails,
                "get_gmail_service_with_token_state",
                return_value=(None, "access-token", None),
            ),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                side_effect=GmailHistoryCursorExpired("cursor expired"),
            ),
            patch.object(
                emails,
                "start_gmail_watch",
                return_value={"historyId": "800", "expiration": "9999999999999"},
            ),
            patch.object(
                emails,
                "list_message_ids",
                side_effect=RuntimeError("Gmail listing failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "Gmail listing failed"),
        ):
            emails.process_realtime_email_update("user@example.com", "124")

        self.assertEqual(user.last_history_id, "123")
        self.assertEqual(user.incremental_sync_status, "failed")

    def test_recovery_upsert_updates_owned_message_without_duplicate(self):
        user = _sync_user(status="completed")
        existing_email = SimpleNamespace(
            id="message-1",
            user_id=user.id,
            subject="Old subject",
        )

        class RecoveryDatabase(FakeDatabase):
            def get_bind(self):
                return SimpleNamespace(
                    dialect=SimpleNamespace(name="sqlite")
                )

            def query(self, *_models):
                return SimpleNamespace(
                    filter=lambda *_conditions: SimpleNamespace(
                        first=lambda: existing_email
                    )
                )

            def add(self, _item):
                raise AssertionError("Recovery must update, not duplicate, the email.")

        emails._upsert_recovered_email(
            RecoveryDatabase(user),
            user,
            {
                "id": "message-1",
                "thread_id": "thread-1",
                "sender": "sender@example.com",
                "subject": "Updated subject",
                "preview": "Preview",
                "body": "Body",
                "body_html": "",
                "has_html": False,
                "received_at": None,
                "is_read": True,
            },
        )

        self.assertEqual(existing_email.subject, "Updated subject")

    def test_duplicate_notifications_process_each_message_only_once(self):
        user = _sync_user(status="completed")
        database = FakeDatabase(user)
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
            patch.object(emails, "SessionLocal", return_value=database),
            patch.object(
                emails,
                "get_gmail_service_with_token_state",
                return_value=(None, "access-token", None),
            ),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                return_value=(["message-1"], "456", "access-token"),
            ) as fetch_history,
            patch.object(emails, "fetch_single_email", return_value=message),
            patch.object(emails, "_store_email_if_new", return_value=True) as store,
            patch.object(emails, "tag_recent_untagged_emails"),
        ):
            emails.process_realtime_email_update("user@example.com", "456")
            emails.process_realtime_email_update("user@example.com", "456")

        self.assertEqual(fetch_history.call_count, 1)
        store.assert_called_once()
        self.assertEqual(user.last_history_id, "456")
        self.assertEqual(user.incremental_sync_status, "completed")

    def test_cursor_does_not_advance_when_email_fetch_fails(self):
        user = _sync_user(status="completed")
        database = FakeDatabase(user)

        with (
            patch.object(emails, "SessionLocal", return_value=database),
            patch.object(
                emails,
                "get_gmail_service_with_token_state",
                return_value=(None, "access-token", None),
            ),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                return_value=(["message-1"], "456", "access-token"),
            ),
            patch.object(
                emails,
                "fetch_single_email",
                side_effect=RuntimeError("Gmail API unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "Gmail API unavailable"),
        ):
            emails.process_realtime_email_update("user@example.com", "456")

        self.assertEqual(user.last_history_id, "123")
        self.assertEqual(user.incremental_sync_status, "failed")

    def test_concurrent_cursor_advance_is_not_overwritten_by_stale_history(self):
        user = _sync_user(status="completed")
        database = FakeDatabase(user)

        def fetch_history(**_kwargs):
            user.last_history_id = "700"
            return [], "650", "access-token"

        with (
            patch.object(emails, "SessionLocal", return_value=database),
            patch.object(
                emails,
                "get_gmail_service_with_token_state",
                return_value=(None, "access-token", None),
            ),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                side_effect=fetch_history,
            ),
            patch.object(emails, "tag_recent_untagged_emails"),
        ):
            emails.process_realtime_email_update("user@example.com", "750")

        self.assertTrue(database.locked)
        self.assertEqual(user.last_history_id, "700")

    def test_late_incremental_failure_cannot_overwrite_newer_success(self):
        user = _sync_user(status="completed")
        user.incremental_sync_id = "old-execution"
        user.incremental_sync_status = "completed"
        database = FakeDatabase(user)

        def fail_after_newer_run_succeeded(**_kwargs):
            user.incremental_sync_id = "new-execution"
            user.incremental_sync_status = "completed"
            user.last_history_id = "700"
            raise RuntimeError("older execution failed after newer success")

        with (
            patch.object(emails, "SessionLocal", return_value=database),
            patch.object(
                emails,
                "get_gmail_service_with_token_state",
                return_value=(None, "access-token", None),
            ),
            patch.object(
                emails,
                "fetch_new_message_ids_from_history",
                side_effect=fail_after_newer_run_succeeded,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "older execution failed after newer success",
            ),
        ):
            emails.process_realtime_email_update(
                "user@example.com",
                "456",
                execution_id="old-execution",
            )

        self.assertEqual(user.incremental_sync_id, "new-execution")
        self.assertEqual(user.incremental_sync_status, "completed")
        self.assertEqual(user.last_history_id, "700")

    def test_gmail_sync_failure_propagates_without_acknowledgment(self):
        user = SimpleNamespace(
            id="user-1",
            access_token="access-token",
            refresh_token="refresh-token",
            token_expiry=None,
            last_history_id="123",
            incremental_sync_id=None,
            incremental_sync_status=None,
        )

        class FakeQuery:
            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeDatabase:
            def query(self, *_args):
                return FakeQuery()

            def commit(self):
                pass

            def rollback(self):
                pass

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
