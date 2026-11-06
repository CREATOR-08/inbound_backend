import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api import dependencies
from app.db.database import (
    InstrumentedQueuePool,
    InstrumentedSession,
    instrument_engine,
    reset_database_request_context,
    set_database_request_context,
)


class DatabasePerformanceInstrumentationTests(unittest.TestCase):
    def test_database_events_log_timings_without_sql_values(self):
        engine = create_engine(
            "sqlite://",
            poolclass=InstrumentedQueuePool,
        )
        instrument_engine(engine)
        session_factory = sessionmaker(bind=engine, class_=InstrumentedSession)
        context_token = set_database_request_context(
            "get_emails",
            "safe-request-id",
        )
        try:
            with self.assertLogs("app.db.database", level="INFO") as captured:
                session = session_factory()
                try:
                    session.execute(
                        text("CREATE TABLE timing_test (value TEXT)")
                    )
                    session.execute(
                        text("INSERT INTO timing_test (value) VALUES (:value)"),
                        {"value": "sensitive-test-value"},
                    )
                    session.commit()

                    result = session.execute(
                        text("SELECT value FROM timing_test")
                    )
                    self.assertEqual(
                        result.fetchone()[0],
                        "sensitive-test-value",
                    )
                    session.rollback()
                finally:
                    session.close()
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))

            logs = "\n".join(captured.output)
            for expected in (
                "event=physical_connection",
                "event=pool_checkout",
                "pool_checked_out=",
                "pool_overflow=",
                "pool_available=",
                "event=sql_execution",
                "event=result_fetch",
                "event=transaction_commit",
                "event=transaction_rollback",
                "endpoint=get_emails",
                "request_id=safe-request-id",
            ):
                self.assertIn(expected, logs)
            self.assertNotIn("sensitive-test-value", logs)
            self.assertNotIn("SELECT value FROM timing_test", logs)
        finally:
            reset_database_request_context(context_token)
            engine.dispose()

    def test_token_persistence_logs_lock_and_total_duration_without_identity(self):
        user = SimpleNamespace(
            access_token="opaque-token",
            token_expiry=None,
            refresh_token=None,
        )

        class FakeQuery:
            def filter(self, *_conditions):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return user

        class FakeDatabase:
            def query(self, *_models):
                return FakeQuery()

            def close(self):
                pass

        context_token = set_database_request_context(
            "get_emails",
            "safe-request-id",
        )
        try:
            with (
                patch.object(
                    dependencies,
                    "SessionLocal",
                    return_value=FakeDatabase(),
                ),
                self.assertLogs("app.db.database", level="INFO") as captured,
            ):
                self.assertTrue(
                    dependencies._persist_account_token_state(
                        "private-user-id",
                        "opaque-token",
                        None,
                        expected_access_token="opaque-token",
                    )
                )

            logs = "\n".join(captured.output)
            self.assertIn("event=token_select_for_update", logs)
            self.assertIn("event=token_persistence", logs)
            self.assertIn("endpoint=get_emails", logs)
            self.assertNotIn("private-user-id", logs)
            self.assertNotIn("opaque-token", logs)
        finally:
            reset_database_request_context(context_token)


if __name__ == "__main__":
    unittest.main()
