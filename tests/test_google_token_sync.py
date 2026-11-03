import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import google_auth
from app.db.database import get_db
from app.main import app


class FakeDatabase:
    def __init__(self):
        self.statement = None
        self.committed = False

    def execute(self, statement):
        self.statement = statement

    def commit(self):
        self.committed = True


class GoogleTokenSyncTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase()

        def get_test_db():
            yield self.database

        self.original_override = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = get_test_db
        self.client = TestClient(app)

    def tearDown(self):
        if self.original_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = self.original_override

    def test_persists_verified_refresh_token_and_expiry(self):
        profile_service = MagicMock()
        profile_service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "person@example.com"
        }

        with (
            patch.dict(os.environ, {"GOOGLE_TOKEN_SYNC_SECRET": "sync-secret"}),
            patch.object(
                google_auth,
                "get_gmail_service_from_token",
                return_value=(profile_service, "access-token"),
            ) as gmail_service,
        ):
            response = self.client.put(
                "/api/v1/auth/google-tokens",
                headers={"X-Google-Token-Sync-Secret": "sync-secret"},
                json={
                    "email": "person@example.com",
                    "access_token": "access-token",
                    "access_token_expires_at": "2026-11-03T18:00:00Z",
                    "refresh_token": "refresh-token",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "synced"})
        self.assertNotIn("refresh-token", response.text)
        gmail_service.assert_called_once_with("access-token")
        self.assertTrue(self.database.committed)

        compiled = self.database.statement.compile(dialect=postgresql.dialect())
        sql = str(compiled).lower()
        self.assertIn("refresh_token", sql)
        self.assertIn("coalesce", sql)
        self.assertIn("nullif", sql)
        self.assertIn("on conflict (email)", sql)
        self.assertIn("token_expiry", sql)
        self.assertIn("refresh-token", compiled.params.values())
        self.assertEqual(compiled.params["initial_sync_status"], "not_started")
        self.assertIs(compiled.params["initial_sync_eligible"], True)
        self.assertNotIn(
            "initial_sync_status",
            sql.split("do update set", maxsplit=1)[1],
        )
        self.assertNotIn(
            "initial_sync_eligible",
            sql.split("do update set", maxsplit=1)[1],
        )
        self.assertEqual(
            compiled.params["token_expiry"],
            datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc),
        )

    def test_preserves_refresh_token_when_missing_null_or_empty(self):
        profile_service = MagicMock()
        profile_service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "person@example.com"
        }

        with (
            patch.dict(os.environ, {"GOOGLE_TOKEN_SYNC_SECRET": "sync-secret"}),
            patch.object(
                google_auth,
                "get_gmail_service_from_token",
                return_value=(profile_service, "access-token"),
            ),
        ):
            for refresh_value in ("missing", None, ""):
                with self.subTest(refresh_value=refresh_value):
                    payload = {
                        "email": "person@example.com",
                        "access_token": "access-token",
                    }
                    if refresh_value != "missing":
                        payload["refresh_token"] = refresh_value

                    response = self.client.put(
                        "/api/v1/auth/google-tokens",
                        headers={"X-Google-Token-Sync-Secret": "sync-secret"},
                        json=payload,
                    )

                    self.assertEqual(response.status_code, 200)
                    compiled = self.database.statement.compile(
                        dialect=postgresql.dialect()
                    )
                    self.assertIsNone(compiled.params["refresh_token"])
                    self.assertIn("coalesce", str(compiled).lower())
                    self.assertIn("nullif", str(compiled).lower())

    def test_rejects_token_sync_if_google_email_does_not_match(self):
        profile_service = MagicMock()
        profile_service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "other@example.com"
        }

        with (
            patch.dict(os.environ, {"GOOGLE_TOKEN_SYNC_SECRET": "sync-secret"}),
            patch.object(
                google_auth,
                "get_gmail_service_from_token",
                return_value=(profile_service, "access-token"),
            ),
        ):
            response = self.client.put(
                "/api/v1/auth/google-tokens",
                headers={"X-Google-Token-Sync-Secret": "sync-secret"},
                json={
                    "email": "person@example.com",
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.database.committed)

    def test_requires_server_sync_secret(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(google_auth, "get_gmail_service_from_token") as gmail_service,
        ):
            with self.assertLogs(google_auth.logger, level="ERROR") as captured:
                response = self.client.put(
                    "/api/v1/auth/google-tokens",
                    headers={"X-Google-Token-Sync-Secret": "sync-secret"},
                    json={
                        "email": "person@example.com",
                        "access_token": "access-token",
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sync-secret", response.text)
        self.assertNotIn("access-token", response.text)
        self.assertEqual(
            captured.output,
            [
                "ERROR:app.api.routes.google_auth:Google token sync unavailable: "
                "server shared secret is not configured"
            ],
        )
        gmail_service.assert_not_called()

    def test_database_failure_is_logged_without_exposing_tokens(self):
        profile_service = MagicMock()
        profile_service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "person@example.com"
        }
        self.database.commit = MagicMock(
            side_effect=SQLAlchemyError("database failure included access-token")
        )

        with (
            patch.dict(os.environ, {"GOOGLE_TOKEN_SYNC_SECRET": "sync-secret"}),
            patch.object(
                google_auth,
                "get_gmail_service_from_token",
                return_value=(profile_service, "access-token"),
            ),
            self.assertLogs(google_auth.logger, level="ERROR") as captured,
        ):
            response = self.client.put(
                "/api/v1/auth/google-tokens",
                headers={"X-Google-Token-Sync-Secret": "sync-secret"},
                json={
                    "email": "person@example.com",
                    "access_token": "access-token",
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertNotIn("access-token", response.text)
        self.assertNotIn("access-token", "\n".join(captured.output))
        self.assertIn("database persistence", "\n".join(captured.output))
        self.assertIn("SQLAlchemyError", "\n".join(captured.output))
