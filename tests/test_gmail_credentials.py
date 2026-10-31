import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api.routes import emails
from app.services import gmail


class GmailCredentialTests(unittest.TestCase):
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
        with patch.object(emails, "SessionLocal", return_value=database):
            emails._persist_gmail_token_state(
                "user-1",
                "refreshed-access-token",
                expiry,
            )

        self.assertTrue(database.committed)
        self.assertEqual(user.access_token, "refreshed-access-token")
        self.assertEqual(user.token_expiry, expiry)
        self.assertEqual(user.refresh_token, "existing-refresh-token")

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

            def first(self):
                nonlocal queries
                queries += 1
                return matching_user

        class FakeDatabase:
            def query(self, *_args):
                return FakeQuery()

        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "user@example.com"
        }
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        from app.api import dependencies

        with patch.object(
            dependencies,
            "get_gmail_service_with_token_state",
            return_value=(service, "refreshed-access-token", expiry),
        ) as service_factory:
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
        self.assertGreaterEqual(queries, 2)


if __name__ == "__main__":
    unittest.main()
