import unittest
from types import SimpleNamespace

from app.api.routes import emails


def _email_data(message_id):
    return {
        "id": message_id,
        "thread_id": "thread",
        "sender": "sender@example.com",
        "subject": "subject",
        "preview": "preview",
        "body": "body",
        "body_html": "",
        "has_html": False,
        "received_at": None,
    }


class _FakeResult:
    def __init__(self, inserted_ids=()):
        self.inserted_ids = inserted_ids

    def scalars(self):
        return self

    def all(self):
        return self.inserted_ids


class _FakeQuery:
    def __init__(self, database):
        self.database = database

    def filter(self, *_conditions):
        return self

    def all(self):
        return [
            (message_id,)
            for message_id in self.database.conflicting_ids
        ]


class _FakePostgresDatabase:
    def __init__(self, conflicting_ids=()):
        self.conflicting_ids = set(conflicting_ids)
        self.statements = []
        self.query_count = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement):
        self.statements.append(statement)
        rows = statement._multi_values[0]
        if statement.table.name == "emails":
            inserted_ids = [
                row[emails.Email.__table__.c.id]
                for row in rows
                if row[emails.Email.__table__.c.id] not in self.conflicting_ids
            ]
            return _FakeResult(inserted_ids)
        return _FakeResult()

    def query(self, *_columns):
        self.query_count += 1
        return _FakeQuery(self)


class InitialEmailBulkPersistenceTests(unittest.TestCase):
    def test_bulk_insert_is_idempotent_and_creates_state_only_for_new_emails(self):
        database = _FakePostgresDatabase(conflicting_ids={"foreign-id"})
        user = SimpleNamespace(id="user-1")

        with self.assertLogs(emails.logger, level="ERROR") as captured:
            imported_count = emails._store_initial_emails(
                database,
                user,
                [
                    _email_data("new-id"),
                    _email_data("foreign-id"),
                    _email_data("new-id"),
                ],
            )

        self.assertEqual(imported_count, 1)
        self.assertEqual(len(database.statements), 2)
        email_insert, state_insert = database.statements
        self.assertEqual(email_insert.table.name, "emails")
        self.assertEqual(
            [
                row[emails.Email.__table__.c.id]
                for row in email_insert._multi_values[0]
            ],
            ["new-id", "foreign-id"],
        )
        self.assertEqual(state_insert.table.name, "email_agent_states")
        self.assertEqual(
            [
                row[emails.EmailAgentStateModel.__table__.c.email_id]
                for row in state_insert._multi_values[0]
            ],
            ["new-id"],
        )
        self.assertIn("ON CONFLICT (id) DO NOTHING", str(email_insert))
        self.assertIn(
            "ON CONFLICT (email_id) DO NOTHING",
            str(state_insert),
        )
        self.assertIn("refusing to reassign", "\n".join(captured.output))
        self.assertNotIn("foreign-id", "\n".join(captured.output))
        self.assertEqual(database.query_count, 1)

    def test_bulk_insert_uses_bounded_batches(self):
        database = _FakePostgresDatabase()
        message_ids = [
            f"message-{index}"
            for index in range(emails.INITIAL_EMAIL_INSERT_BATCH_SIZE + 1)
        ]

        imported_count = emails._store_initial_emails(
            database,
            SimpleNamespace(id="user-1"),
            [_email_data(message_id) for message_id in message_ids],
        )

        self.assertEqual(imported_count, len(message_ids))
        email_inserts = [
            statement
            for statement in database.statements
            if statement.table.name == "emails"
        ]
        state_inserts = [
            statement
            for statement in database.statements
            if statement.table.name == "email_agent_states"
        ]
        self.assertEqual(len(email_inserts), 2)
        self.assertEqual(len(state_inserts), 2)
        self.assertEqual(database.query_count, 0)
        self.assertTrue(
            all(
                len(statement._multi_values[0])
                <= emails.INITIAL_EMAIL_INSERT_BATCH_SIZE
                for statement in email_inserts
            )
        )


if __name__ == "__main__":
    unittest.main()
