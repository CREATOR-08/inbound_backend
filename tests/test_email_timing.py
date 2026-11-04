import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.routes import emails
from app.main import app


class _FakeDatabase:
    def rollback(self):
        pass

    def close(self):
        pass


class EmailEndpointTimingTests(unittest.TestCase):
    def test_timed_email_endpoints_pass_context_and_log_endpoint_duration(self):
        endpoints = (
            (
                "GET",
                "/api/v1/emails/sync-initial/status",
                "get_initial_sync_status",
            ),
            ("POST", "/api/v1/emails/sync-initial", "sync_initial_emails"),
            ("GET", "/api/v1/emails", "get_emails"),
        )

        def override_get_db():
            yield _FakeDatabase()

        app.dependency_overrides[emails.get_db] = override_get_db
        try:
            with TestClient(app) as client:
                for method, path, endpoint in endpoints:
                    with self.subTest(endpoint=endpoint):
                        with (
                            patch.object(
                                emails,
                                "_resolve_user_account",
                                side_effect=HTTPException(
                                    status_code=401,
                                    detail="Invalid test credentials.",
                                ),
                            ) as resolver,
                            self.assertLogs("app.main", level="INFO") as captured,
                        ):
                            response = client.request(
                                method,
                                path,
                                headers={"Authorization": "Bearer safe-test-token"},
                            )

                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(
                            resolver.call_args.kwargs["timing_context"],
                            endpoint,
                        )
                        logs = "\n".join(captured.output)
                        self.assertIn(f"endpoint={endpoint}", logs)
                        self.assertIn("total_endpoint_duration_ms=", logs)
                        self.assertNotIn("safe-test-token", logs)
        finally:
            app.dependency_overrides.pop(emails.get_db, None)


if __name__ == "__main__":
    unittest.main()
