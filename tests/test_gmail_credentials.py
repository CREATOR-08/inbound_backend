import re
import unittest
from datetime import datetime, timedelta, timezone
from time import sleep
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from googleapiclient.errors import HttpError

from app.api import dependencies
from app.api.google_profile_cache import (
    google_profile_verification_cache,
)
from app.api.routes import emails
from app.services import gmail


class GmailCredentialTests(unittest.TestCase):
    def setUp(self):
        google_profile_verification_cache.clear()

    def test_new_account_insert_is_conflict_safe_and_marks_initial_eligibility(self):
        created_user = SimpleNamespace(
            id="user@example.com",
            email="user@example.com",
            access_token="access-token",
            refresh_token=None,
            token_expiry=None,
            initial_sync_status="not_started",
            initial_sync_eligible=True,
        )

        class FakeQuery:
            def __init__(self, database):
                self.database = database

            def filter(self, *_conditions):
                self.database.query_count += 1
                return self

            def first(self):
                if self.database.query_count <= 2:
                    return None
                return created_user

        class FakeDatabase:
            query_count = 0
            statement = None

            def query(self, *_models):
                return FakeQuery(self)

            def get_bind(self):
                return SimpleNamespace(
                    dialect=SimpleNamespace(name="postgresql")
                )

            def execute(self, statement):
                self.statement = statement
                return SimpleNamespace(
                    scalar_one_or_none=lambda: "user@example.com"
                )

            def rollback(self):
                pass

        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "user@example.com"
        }
        database = FakeDatabase()
        with patch.object(
            dependencies,
            "get_gmail_service_from_token",
            return_value=(service, "access-token"),
        ):
            account, active_token = dependencies._resolve_user_account(
                "access-token",
                database,
                create=True,
            )

        compiled_sql = str(database.statement.compile()).lower()
        self.assertIn("on conflict do nothing", compiled_sql)
        self.assertEqual(account.initial_sync_status, "not_started")
        self.assertTrue(account.initial_sync_eligible)
        self.assertEqual(active_token, "access-token")

    def test_history_api_follows_all_pages_and_deduplicates_message_ids(self):
        service = MagicMock()
        history_list = service.users.return_value.history.return_value.list
        history_list.return_value.execute.side_effect = [
            {
                "historyId": "456",
                "history": [
                    {
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "message-1",
                                    "labelIds": ["INBOX"],
                                }
                            },
                            {
                                "message": {
                                    "id": "message-2",
                                    "labelIds": ["SENT"],
                                }
                            },
                        ]
                    }
                ],
                "nextPageToken": "next-page",
            },
            {
                "historyId": "789",
                "history": [
                    {
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "message-1",
                                    "labelIds": ["INBOX"],
                                }
                            },
                            {
                                "message": {
                                    "id": "message-3",
                                    "labelIds": ["INBOX"],
                                }
                            },
                        ]
                    }
                ],
            },
        ]

        with patch.object(
            gmail,
            "get_gmail_service_from_token",
            return_value=(service, "access-token"),
        ):
            result = gmail.fetch_new_message_ids_from_history(
                access_token="access-token",
                start_history_id="123",
            )

        self.assertEqual(set(result[0]), {"message-1", "message-3"})
        self.assertEqual(result[1:], ("789", "access-token"))
        self.assertEqual(history_list.call_count, 2)
        self.assertIsNone(history_list.call_args_list[0].kwargs["pageToken"])
        self.assertEqual(
            history_list.call_args_list[1].kwargs["pageToken"],
            "next-page",
        )

    def test_credentials_include_refresh_configuration_and_account_expiry(self):
        expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        credentials = MagicMock()
        credentials.expired = False
        credentials.token = "access-token"
        credentials.expiry = expiry

        with (
            patch.object(gmail, "Credentials", return_value=credentials) as factory,
            patch.object(gmail, "build", return_value="gmail-service"),
            patch.object(gmail, "GOOGLE_CLIENT_ID", "configured-client-id"),
            patch.object(gmail, "GOOGLE_CLIENT_SECRET", "configured-client-secret"),
        ):
            result = gmail.get_gmail_service_with_token_state(
                "access-token",
                "refresh-token",
                expiry,
            )

        self.assertEqual(result, ("gmail-service", "access-token", expiry))
        self.assertEqual(
            factory.call_args.kwargs,
            {
                "token": "access-token",
                "refresh_token": "refresh-token",
                "token_uri": gmail.GMAIL_TOKEN_URI,
                "client_id": "configured-client-id",
                "client_secret": "configured-client-secret",
                "expiry": expiry.replace(tzinfo=None),
            },
        )

    def test_timezone_aware_expiry_is_normalized_for_google_credentials(self):
        expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

        with patch.object(gmail, "build", return_value="gmail-service") as build:
            service, active_token, returned_expiry = (
                gmail.get_gmail_service_with_token_state(
                    "access-token",
                    token_expiry=expiry,
                )
            )

        google_credentials = build.call_args.kwargs["credentials"]
        self.assertIsNone(google_credentials.expiry.tzinfo)
        self.assertEqual(service, "gmail-service")
        self.assertEqual(active_token, "access-token")
        self.assertEqual(returned_expiry, expiry)

    def test_expired_credentials_refresh_and_return_new_token_state(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        credentials = MagicMock()
        credentials.expired = True
        credentials.token = "refreshed-access-token"
        credentials.refresh_token = "refresh-token"
        credentials.expiry = expiry
        refresh_callback = MagicMock()

        with (
            patch.object(gmail, "Credentials", return_value=credentials),
            patch.object(gmail, "Request", return_value="google-auth-request"),
            patch.object(gmail, "build", return_value="gmail-service"),
            patch.object(gmail, "GOOGLE_CLIENT_ID", "client-id"),
            patch.object(gmail, "GOOGLE_CLIENT_SECRET", "client-secret"),
        ):
            result = gmail.get_gmail_service_with_token_state(
                "old-access-token",
                "refresh-token",
                datetime.now(timezone.utc) - timedelta(minutes=5),
                on_token_refresh=refresh_callback,
            )

        credentials.refresh.assert_called_once_with("google-auth-request")
        refresh_callback.assert_called_once_with(
            "refreshed-access-token",
            expiry,
            "refresh-token",
        )
        self.assertEqual(
            result,
            ("gmail-service", "refreshed-access-token", expiry),
        )

    def test_expired_credentials_without_refresh_token_require_reauthorization(self):
        credentials = MagicMock()
        credentials.expired = True

        with (
            patch.object(gmail, "Credentials", return_value=credentials),
            patch.object(gmail, "build") as build,
            self.assertRaises(gmail.GmailReauthorizationRequired),
        ):
            gmail.get_gmail_service_with_token_state(
                "old-access-token",
                token_expiry=datetime.now(timezone.utc) - timedelta(minutes=5),
            )

        credentials.refresh.assert_not_called()
        build.assert_not_called()

    def test_refreshed_token_state_persists_without_changing_refresh_token(self):
        user = SimpleNamespace(
            id="user-1",
            access_token="old-access-token",
            refresh_token="existing-refresh-token",
            token_expiry=None,
        )

        class FakeQuery:
            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeDatabase:
            committed = False

            def query(self, *_args):
                return FakeQuery()

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

            def close(self):
                pass

        database = FakeDatabase()
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        with patch.object(dependencies, "SessionLocal", return_value=database):
            emails._persist_gmail_token_state(
                "user-1",
                "refreshed-access-token",
                expiry,
            )

        self.assertTrue(database.committed)
        self.assertEqual(user.access_token, "refreshed-access-token")
        self.assertEqual(user.token_expiry, expiry)
        self.assertEqual(user.refresh_token, "existing-refresh-token")

    def test_refreshed_token_state_persists_replacement_refresh_token(self):
        user = SimpleNamespace(
            id="user-1",
            access_token="old-access-token",
            refresh_token="old-refresh-token",
            token_expiry=None,
        )

        class FakeQuery:
            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeDatabase:
            committed = False

            def query(self, *_args):
                return FakeQuery()

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

            def close(self):
                pass

        database = FakeDatabase()
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        with patch.object(dependencies, "SessionLocal", return_value=database):
            persisted = dependencies._persist_account_token_state(
                "user-1",
                "new-access-token",
                expiry,
                "new-refresh-token",
                expected_access_token="old-access-token",
            )

        self.assertTrue(persisted)
        self.assertTrue(database.committed)
        self.assertEqual(user.access_token, "new-access-token")
        self.assertEqual(user.token_expiry, expiry)
        self.assertEqual(user.refresh_token, "new-refresh-token")

    def test_stale_refresh_cannot_overwrite_newer_account_token_state(self):
        user = SimpleNamespace(
            id="user-1",
            access_token="current-access-token",
            refresh_token="current-refresh-token",
            token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        class FakeQuery:
            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeDatabase:
            committed = False

            def query(self, *_args):
                return FakeQuery()

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

            def close(self):
                pass

        database = FakeDatabase()
        original_expiry = user.token_expiry
        with patch.object(dependencies, "SessionLocal", return_value=database):
            persisted = dependencies._persist_account_token_state(
                "user-1",
                "stale-access-token",
                datetime.now(timezone.utc) + timedelta(hours=2),
                "stale-refresh-token",
                expected_access_token="old-access-token",
            )

        self.assertFalse(persisted)
        self.assertFalse(database.committed)
        self.assertEqual(user.access_token, "current-access-token")
        self.assertEqual(user.refresh_token, "current-refresh-token")
        self.assertEqual(user.token_expiry, original_expiry)

    def test_assistant_auth_persists_rotated_refresh_token_after_expiry(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            access_token="old-access-token",
            refresh_token="old-refresh-token",
            token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
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

            def expire(self, _user):
                pass

            def refresh(self, _user):
                pass

        databases = [FakeDatabase() for _ in range(4)]
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "user@example.com"
        }

        def refresh_credentials(*_args, **kwargs):
            kwargs["on_token_refresh"](
                "new-access-token",
                expiry,
                "new-refresh-token",
            )
            return service, "new-access-token", expiry

        with (
            patch.object(dependencies, "SessionLocal", side_effect=databases),
            patch.object(
                dependencies,
                "get_gmail_service_with_token_state",
                side_effect=refresh_credentials,
            ),
        ):
            authenticated_user = dependencies.get_current_user(
                authorization="Bearer old-access-token"
            )

        self.assertEqual(user.access_token, "new-access-token")
        self.assertEqual(user.token_expiry, expiry)
        self.assertEqual(user.refresh_token, "new-refresh-token")
        self.assertEqual(authenticated_user.id, "user-1")
        self.assertFalse(hasattr(authenticated_user, "refresh_token"))
        self.assertFalse(hasattr(authenticated_user, "access_token"))

    def test_invalid_refresh_token_returns_reauthorization_required(self):
        user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            access_token="old-access-token",
            refresh_token="revoked-refresh-token",
            token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
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
            patch.object(
                dependencies,
                "SessionLocal",
                return_value=FakeDatabase(),
            ),
            patch.object(
                dependencies,
                "get_gmail_service_with_token_state",
                side_effect=gmail.GmailReauthorizationRequired("revoked"),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            dependencies.get_current_user(
                authorization="Bearer old-access-token"
            )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("reauthorized", raised.exception.detail.lower())
        self.assertNotIn("revoked-refresh-token", raised.exception.detail)

    def test_history_sync_uses_refreshed_access_token(self):
        response = {
            "historyId": "456",
            "history": [
                {
                    "messagesAdded": [
                        {"message": {"id": "message-1", "labelIds": ["INBOX"]}}
                    ]
                }
            ],
        }
        service = MagicMock()
        (
            service.users.return_value.history.return_value.list.return_value
            .execute.return_value
        ) = response

        with patch.object(
            gmail,
            "get_gmail_service_from_token",
            return_value=(service, "refreshed-access-token"),
        ) as service_factory:
            result = gmail.fetch_new_message_ids_from_history(
                "old-access-token",
                "123",
                "refresh-token",
            )

        service_factory.assert_called_once_with(
            "old-access-token",
            "refresh-token",
            None,
            None,
        )
        self.assertEqual(
            result,
            (["message-1"], "456", "refreshed-access-token"),
        )

    def test_user_resolution_uses_only_the_matching_accounts_refresh_token(self):
        matching_user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            access_token="old-access-token",
            refresh_token="user-1-refresh",
            token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        other_user = SimpleNamespace(
            id="user-2",
            email="other@example.com",
            access_token="other-access-token",
            refresh_token="other-refresh-token",
            token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        queries = 0

        class FakeQuery:
            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                nonlocal queries
                queries += 1
                return matching_user

        class FakeDatabase:
            def query(self, *_args):
                return FakeQuery()

            def rollback(self):
                pass

            def expire(self, _user):
                pass

            def refresh(self, _user, with_for_update=False):
                pass

        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "user@example.com"
        }
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        from app.api import dependencies

        def persist_token_state(
            user_id,
            access_token,
            token_expiry,
            refresh_token=None,
            *,
            expected_access_token,
        ):
            self.assertEqual(user_id, matching_user.id)
            self.assertEqual(expected_access_token, "old-access-token")
            matching_user.access_token = access_token
            matching_user.token_expiry = token_expiry
            if refresh_token:
                matching_user.refresh_token = refresh_token
            return True

        with patch.object(
            dependencies,
            "get_gmail_service_with_token_state",
            return_value=(service, "refreshed-access-token", expiry),
        ) as service_factory, patch.object(
            dependencies,
            "_persist_account_token_state",
            side_effect=persist_token_state,
        ) as persist_state:
            user, active_token = dependencies._resolve_user_account(
                "old-access-token",
                FakeDatabase(),
            )

        self.assertIs(user, matching_user)
        self.assertEqual(active_token, "refreshed-access-token")
        self.assertEqual(matching_user.access_token, "refreshed-access-token")
        self.assertEqual(matching_user.token_expiry, expiry)
        self.assertEqual(matching_user.refresh_token, "user-1-refresh")
        self.assertNotEqual(matching_user.refresh_token, other_user.refresh_token)
        self.assertEqual(service_factory.call_args.args[1], "user-1-refresh")
        persist_state.assert_called_once()
        self.assertGreaterEqual(queries, 2)

    def test_user_resolution_reuses_cached_profile_for_matching_account(self):
        user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            access_token="access-token",
            refresh_token=None,
            token_expiry=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

        class FakeQuery:
            def filter(self, *_args):
                return self

            def first(self):
                return user

        class FakeDatabase:
            def query(self, *_args):
                return FakeQuery()

            def rollback(self):
                pass

            def expire(self, _user):
                pass

            def refresh(self, _user):
                pass

        service = MagicMock()

        def delayed_profile():
            sleep(0.03)
            return {"emailAddress": "user@example.com"}

        service.users.return_value.getProfile.return_value.execute.side_effect = (
            delayed_profile
        )

        with (
            patch.object(
                dependencies,
                "get_gmail_service_with_token_state",
                return_value=(service, "access-token", user.token_expiry),
            ),
            patch.object(
                dependencies,
                "_persist_account_token_state",
                return_value=True,
            ),
            self.assertLogs(dependencies.logger, level="INFO") as captured,
        ):
            for _ in range(2):
                resolved_user, active_token = dependencies._resolve_user_account(
                    "access-token",
                    FakeDatabase(),
                    timing_context="test",
                )
                self.assertIs(resolved_user, user)
                self.assertEqual(active_token, "access-token")

        profile_request = service.users.return_value.getProfile.return_value
        profile_request.execute.assert_called_once_with()
        logs = "\n".join(captured.output)
        self.assertIn("google_profile_calls=1", logs)
        self.assertIn("google_profile_calls=0", logs)
        self.assertIn("profile_cache_hit=True", logs)
        profile_durations = [
            float(value)
            for value in re.findall(r"google_profile_ms=([0-9.]+)", logs)
        ]
        self.assertEqual(len(profile_durations), 2)
        self.assertGreaterEqual(profile_durations[0], 20)
        self.assertLess(profile_durations[1], 20)

    def test_invalid_google_profile_response_is_not_cached(self):
        class FakeQuery:
            def filter(self, *_args):
                return self

            def first(self):
                return None

        class FakeDatabase:
            def query(self, *_args):
                return FakeQuery()

            def rollback(self):
                pass

        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.side_effect = (
            HttpError(
                SimpleNamespace(status=401, reason="Unauthorized"),
                b"unauthorized",
            )
        )

        with (
            patch.object(
                dependencies,
                "get_gmail_service_from_token",
                return_value=(service, "invalid-token"),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            dependencies._resolve_user_account(
                "invalid-token",
                FakeDatabase(),
            )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIsNone(
            google_profile_verification_cache.get(
                "invalid-token",
                "user@example.com",
            )
        )
        service.users.return_value.getProfile.return_value.execute.assert_called_once()

    def test_google_403_invalidates_cached_profile_identity(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value.execute.side_effect = (
            HttpError(
                SimpleNamespace(status=403, reason="Forbidden"),
                b"forbidden",
            )
        )
        google_profile_verification_cache.set(
            "access-token",
            "user@example.com",
        )

        with (
            patch.object(
                gmail,
                "get_gmail_service_from_token",
                return_value=(service, "access-token"),
            ),
            self.assertRaises(HttpError),
        ):
            gmail.list_message_ids("access-token", "in:inbox")

        self.assertIsNone(
            google_profile_verification_cache.get(
                "access-token",
                "user@example.com",
            )
        )

    def test_token_replacement_invalidates_old_and_new_profile_entries(self):
        user = SimpleNamespace(
            id="user-1",
            access_token="old-access-token",
            refresh_token="refresh-token",
            token_expiry=None,
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

        google_profile_verification_cache.set(
            "old-access-token", "user@example.com"
        )
        google_profile_verification_cache.set(
            "new-access-token", "user@example.com"
        )
        with patch.object(
            dependencies,
            "SessionLocal",
            return_value=FakeDatabase(),
        ):
            dependencies._persist_account_token_state(
                "user-1",
                "new-access-token",
                datetime.now(timezone.utc) + timedelta(minutes=10),
            )

        self.assertIsNone(
            google_profile_verification_cache.get(
                "old-access-token",
                "user@example.com",
            )
        )
        self.assertIsNone(
            google_profile_verification_cache.get(
                "new-access-token",
                "user@example.com",
            )
        )


if __name__ == "__main__":
    unittest.main()
