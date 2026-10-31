import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.Assistant import nodes
from app.agent.Assistant.config import get_model
from app.agent.Assistant.graph import assistant_graph
from app.api import dependencies
from app.api.dependencies import AuthenticatedUser
from app.api.schemas.assistant import (
    AssistantResponse,
    EmailFilter,
    EmailQuerySpec,
    PlannedUIAction,
)
from app.db.database import Base
from app.db.models import Email, UserAccount
from app.services.assistant_queries import get_email_for_user, search_emails


class AssistantGraphTests(unittest.TestCase):
    def test_graph_routes_data_query(self):
        query_plan = nodes.DataQueryPlan(
            source="local_email_store",
            query=EmailQuerySpec(),
            message="Local emails",
        )
        with (
            patch.object(
                nodes,
                "_invoke_structured",
                side_effect=[
                    nodes.IntentResult(intent="DATA_QUERY", reason="Search"),
                    query_plan,
                ],
            ),
            patch.object(nodes, "search_emails", return_value={"items": [], "total": 0}),
        ):
            result = assistant_graph.invoke({
                "user_id": "user-1",
                "session_id": "session-1",
                "message": "Show my emails",
            })
        self.assertEqual(result["response"]["intent"], "DATA_QUERY")
        self.assertEqual(result["response"]["data"]["total"], 0)

    def test_graph_routes_ui_action_and_validates_it(self):
        action_plan = nodes.UIActionPlan(
            message="Opening the inbox.",
            actions=[
                PlannedUIAction(
                    type="NAVIGATE",
                    parameters={"page": "inbox"},
                    description="Open inbox",
                )
            ],
        )
        with patch.object(
            nodes,
            "_invoke_structured",
            side_effect=[
                nodes.IntentResult(intent="UI_ACTION", reason="Navigate"),
                action_plan,
            ],
        ):
            result = assistant_graph.invoke({
                "user_id": "user-1",
                "session_id": "session-1",
                "message": "Open my inbox",
            })
        self.assertEqual(result["response"]["intent"], "UI_ACTION")
        self.assertEqual(result["response"]["actions"][0]["type"], "NAVIGATE")
        AssistantResponse.model_validate(result["response"])

    def test_graph_routes_grounded_question(self):
        with (
            patch.object(
                nodes,
                "_invoke_structured",
                return_value=nodes.IntentResult(intent="QUESTION", reason="Question"),
            ),
            patch.object(nodes, "_invoke_text", return_value="The sender requests a reply."),
        ):
            result = assistant_graph.invoke({
                "user_id": "user-1",
                "session_id": "session-1",
                "message": "What is the sender asking?",
            })
        self.assertEqual(result["response"]["intent"], "QUESTION")
        self.assertEqual(result["response"]["message"], "The sender requests a reply.")

    def test_question_fetches_selected_email_only_when_relevant(self):
        state = {
            "user_id": "u1",
            "message": "What are good email habits?",
            "page_context": {
                "page": "inbox",
                "selected_email_id": "e1",
            },
        }
        with patch.object(nodes, "get_email_for_user") as get_email:
            update = nodes.fetch_context(state)
        get_email.assert_not_called()
        self.assertEqual(update["retrieved_context"], {})

        with patch.object(
            nodes, "get_email_for_user", return_value={"id": "e1"}
        ) as get_email:
            update = nodes.fetch_context({
                **state,
                "message": "Summarize this email",
            })
        get_email.assert_called_once_with("u1", "e1")
        self.assertIn("selected_email", update["retrieved_context"])

    def test_graph_routes_workflow_and_executes_safe_search(self):
        query = EmailQuerySpec(limit=5)
        plan = nodes.WorkflowPlan(
            summary="Search for recent email.",
            steps=[
                nodes.WorkflowStep(
                    tool="search_emails",
                    parameters={"query": query.model_dump(mode="json")},
                    description="Search locally stored emails",
                )
            ],
        )
        with (
            patch.object(
                nodes,
                "_invoke_structured",
                side_effect=[
                    nodes.IntentResult(intent="WORKFLOW", reason="Multi-step task"),
                    plan,
                ],
            ),
            patch.object(nodes, "search_emails", return_value={"items": [], "total": 0}),
        ):
            result = assistant_graph.invoke({
                "user_id": "user-1",
                "session_id": "session-1",
                "message": "Find a recent email",
            })
        self.assertEqual(result["response"]["intent"], "WORKFLOW")
        self.assertEqual(result["response"]["data"]["results"][0]["status"], "completed")

    def test_graph_clarifies_classifier_result(self):
        with patch.object(
            nodes,
            "_invoke_structured",
            return_value=nodes.IntentResult(intent="CLARIFICATION", reason="Unclear"),
        ):
            result = assistant_graph.invoke({
                "user_id": "user-1",
                "session_id": "session-1",
                "message": "Do something",
            })
        self.assertTrue(result["response"]["requires_clarification"])
        self.assertEqual(result["response"]["intent"], "CLARIFICATION")

    def test_classifier_rate_limit_is_explicit(self):
        with patch.object(
            nodes, "_invoke_structured", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")
        ):
            update = nodes.classify_intent({"message": "hello"})
        self.assertEqual(update["response"]["status"], "rate_limited")
        self.assertTrue(update["response"]["requires_clarification"])

    def test_classifier_failure_is_explicit(self):
        with patch.object(nodes, "_invoke_structured", side_effect=TimeoutError("timeout")):
            update = nodes.classify_intent({"message": "hello"})
        self.assertEqual(update["response"]["status"], "error")
        self.assertIn("failed", update["error"])

    def test_query_planner_refuses_remote_gmail_fetch(self):
        plan = nodes.DataQueryPlan(
            source="gmail",
            query=EmailQuerySpec(),
            message="Fetch new Gmail",
        )
        with patch.object(nodes, "_invoke_structured", return_value=plan):
            update = nodes.plan_data_query({
                "user_id": "user-1",
                "message": "Fetch today's Gmail",
            })
        self.assertEqual(update["response"]["status"], "unsupported")
        self.assertIsNone(update.get("query_spec"))

    def test_email_query_schema_matches_model_columns_and_allowlist(self):
        description = nodes.describe_email_query_schema()
        columns = {column["name"]: column for column in description["columns"]}

        self.assertEqual(set(columns), {column.name for column in Email.__table__.columns})
        for model_column in Email.__table__.columns:
            description_column = columns[model_column.name]
            self.assertEqual(description_column["type"], repr(model_column.type))
            self.assertEqual(description_column["nullable"], model_column.nullable)
            self.assertEqual(description_column["primary_key"], model_column.primary_key)
            self.assertIn("meaning", description_column)

        query_fields = description["query_spec"]["query_fields"]
        self.assertEqual(
            set(query_fields),
            {"sender", "subject", "priority", "category", "received_at", "is_archived"},
        )
        self.assertEqual(query_fields["sender"]["operators"], ["eq", "contains", "in"])
        self.assertEqual(query_fields["subject"]["operators"], ["eq", "contains", "in"])
        self.assertEqual(query_fields["priority"]["operators"], ["eq", "in"])
        self.assertEqual(query_fields["category"]["operators"], ["eq", "in"])
        self.assertEqual(
            query_fields["received_at"]["operators"],
            ["eq", "gt", "gte", "lt", "lte", "between"],
        )
        self.assertEqual(query_fields["is_archived"]["operators"], ["eq", "in"])
        self.assertEqual(query_fields["priority"]["allowed_values"], ["HIGH", "MEDIUM", "LOW"])
        self.assertEqual(
            query_fields["category"]["allowed_values"],
            ["MEETING_REQUEST", "ACTION_REQUIRED", "INFORMATIONAL", "SPAM"],
        )
        self.assertEqual(query_fields["received_at"]["model_column"], "created_at")
        self.assertIn("email_agent_states.category", query_fields["category"]["effective_value"])
        self.assertIn("email_agent_states.priority", query_fields["priority"]["effective_value"])
        self.assertTrue(columns["created_at"]["queryable"])
        self.assertEqual(columns["created_at"]["query_field"], "received_at")

    def test_email_query_schema_marks_unsupported_model_fields_non_queryable(self):
        description = nodes.describe_email_query_schema()
        columns = {column["name"]: column for column in description["columns"]}
        query_fields = description["query_spec"]["query_fields"]

        for name in ("id", "thread_id", "user_id", "preview", "body", "body_html", "has_html"):
            self.assertFalse(columns[name]["queryable"], name)
        self.assertNotIn("urgency", columns)
        self.assertNotIn("requires_action", columns)
        self.assertNotIn("urgency", query_fields)
        self.assertNotIn("requires_action", query_fields)
        self.assertFalse(columns["user_id"]["queryable"])
        self.assertIn("authenticated identity", columns["user_id"]["meaning"])

    def test_query_planner_prompt_receives_schema_and_field_semantics(self):
        plan = nodes.DataQueryPlan(
            source="local_email_store",
            query=EmailQuerySpec(),
            message="Searching locally stored emails.",
        )
        with (
            patch.object(nodes, "_invoke_structured", return_value=plan) as model,
            patch.object(nodes, "search_emails", return_value={"items": [], "total": 0}),
        ):
            nodes.plan_data_query({"user_id": "user-1", "message": "Show emails"})

        prompt = model.call_args.args[2]
        self.assertIn(
            json.dumps(nodes.describe_email_query_schema(), ensure_ascii=False),
            prompt,
        )
        self.assertIn("ACTION_REQUIRED is a supported category value, never a priority value", prompt)
        self.assertIn("Urgency and requires_action are not Email columns", prompt)
        self.assertIn("Do not include user_id", prompt)
        self.assertIn("Do not generate SQL", prompt)
        self.assertIn("request clarification", prompt)
        self.assertIn("Do not claim that matching emails exist", prompt)

    def test_query_planner_uses_priority_field_for_high_priority(self):
        query = EmailQuerySpec(
            filters=[EmailFilter(field="priority", operator="eq", value="HIGH")]
        )
        plan = nodes.DataQueryPlan(
            source="local_email_store",
            query=query,
            message="Searching locally stored emails.",
        )
        with (
            patch.object(nodes, "_invoke_structured", return_value=plan),
            patch.object(nodes, "search_emails", return_value={"items": [], "total": 0}),
        ):
            update = nodes.plan_data_query({
                "user_id": "user-1",
                "message": "Show high-priority emails",
            })

        self.assertEqual(
            update["query_spec"]["filters"],
            [{"field": "priority", "operator": "eq", "value": "HIGH"}],
        )
        EmailQuerySpec.model_validate(update["query_spec"])

    def test_query_planner_uses_category_for_action_required_emails(self):
        query = EmailQuerySpec(
            filters=[EmailFilter(field="category", operator="eq", value="ACTION_REQUIRED")]
        )
        plan = nodes.DataQueryPlan(
            source="local_email_store",
            query=query,
            message="Searching locally stored emails.",
        )
        with (
            patch.object(nodes, "_invoke_structured", return_value=plan),
            patch.object(nodes, "search_emails", return_value={"items": [], "total": 0}),
        ):
            update = nodes.plan_data_query({
                "user_id": "user-1",
                "message": "Show action-required emails",
            })

        self.assertEqual(
            update["query_spec"]["filters"],
            [{"field": "category", "operator": "eq", "value": "ACTION_REQUIRED"}],
        )
        self.assertNotEqual(update["query_spec"]["filters"][0]["field"], "priority")

    def test_query_planner_does_not_invent_urgency_field(self):
        plan = nodes.DataQueryPlan(
            source="local_email_store",
            query=EmailQuerySpec(),
            message="Urgency is not supported as an email query field.",
            unsupported_features=["urgency"],
            requires_clarification=True,
        )
        with (
            patch.object(nodes, "_invoke_structured", return_value=plan),
            patch.object(nodes, "search_emails") as search,
        ):
            update = nodes.plan_data_query({
                "user_id": "user-1",
                "message": "Show urgent emails",
            })

        self.assertEqual(update["response"]["status"], "unsupported")
        self.assertTrue(update["response"]["requires_clarification"])
        search.assert_not_called()

    def test_query_validation_rejects_unsupported_priority_and_urgency(self):
        with self.assertRaises(ValueError):
            EmailFilter.model_validate({
                "field": "priority",
                "operator": "eq",
                "value": "ACTION_REQUIRED",
            })
        with self.assertRaises(ValueError):
            EmailFilter.model_validate({
                "field": "urgency",
                "operator": "eq",
                "value": "HIGH",
            })
        with self.assertRaises(ValueError):
            EmailQuerySpec.model_validate({"user_id": "another-user"})

    def test_unread_request_is_rejected_without_query_model_call(self):
        with patch.object(nodes, "_invoke_structured") as model:
            update = nodes.plan_data_query({
                "user_id": "u1",
                "message": "Show unread emails",
            })
        model.assert_not_called()
        self.assertEqual(update["response"]["status"], "unsupported")

    def test_next_page_reuses_validated_query_without_model_call(self):
        context = EmailQuerySpec(
            filters=[EmailFilter(field="subject", operator="contains", value="report")],
            limit=10,
            offset=0,
        )
        with (
            patch.object(nodes, "_invoke_structured") as model,
            patch.object(
                nodes,
                "search_emails",
                return_value={
                    "items": [],
                    "total": 12,
                    "limit": 10,
                    "offset": 10,
                    "next_offset": None,
                },
            ) as search,
        ):
            update = nodes.plan_data_query({
                "user_id": "u1",
                "message": "next page",
                "query_context": context.model_dump(mode="json"),
            })
        model.assert_not_called()
        self.assertEqual(update["query_spec"]["offset"], 10)
        search.assert_called_once()

    def test_ui_action_schema_rejects_javascript(self):
        with self.assertRaises(ValueError):
            PlannedUIAction(
                type="NAVIGATE",
                parameters={"javascript": "alert(1)"},
                description="bad",
            )

    def test_response_validation_rejects_unknown_action(self):
        update = nodes.validate_response({
            "intent": "UI_ACTION",
            "response": {
                "message": "bad",
                "actions": [{"type": "RUN_JS", "parameters": {"code": "x"}}],
            },
        })
        self.assertEqual(update["response"]["status"], "error")

    def test_workflow_send_requires_explicit_confirmation(self):
        step = {
            "tool": "send_email_reply",
            "parameters": {"email_id": "e1", "body": "Hello"},
            "description": "Send reply",
        }
        state = {
            "user_id": "u1",
            "workflow_plan": [step],
            "confirm_workflow": False,
        }
        with patch.object(nodes, "send_email_reply") as send_reply:
            response = nodes.execute_tools(state)["response"]
        send_reply.assert_not_called()
        self.assertEqual(response["status"], "awaiting_confirmation")

    def test_confirmed_workflow_sends_only_scoped_email(self):
        step = {
            "tool": "send_email_reply",
            "parameters": {"email_id": "e1", "body": "Hello"},
            "description": "Send reply",
        }
        with (
            patch.object(
                nodes,
                "get_email_for_user",
                return_value={
                    "id": "e1",
                    "sender": "owner@example.com",
                    "subject": "Original",
                    "thread_id": "thread1",
                    "body": "Message",
                },
            ) as get_email,
            patch.object(nodes, "send_email_reply", return_value="sent-id") as send_reply,
        ):
            response = nodes.execute_tools({
                "user_id": "u1",
                "user_account": {"access_token": "token"},
                "approved_workflow": [step],
                "confirm_workflow": True,
            })["response"]
        get_email.assert_called_once_with("u1", "e1")
        send_reply.assert_called_once()
        self.assertEqual(response["status"], "confirmed")
        self.assertEqual(
            response["data"]["results"][0]["data"]["message_id"], "sent-id"
        )

    def test_workflow_does_not_claim_failed_send_completed(self):
        step = {
            "tool": "send_email_reply",
            "parameters": {"email_id": "e1", "body": "Hello"},
            "description": "Send reply",
        }
        with (
            patch.object(
                nodes,
                "get_email_for_user",
                return_value={
                    "id": "e1",
                    "sender": "owner@example.com",
                    "subject": "Original",
                    "thread_id": "thread1",
                    "body": "Message",
                },
            ),
            patch.object(nodes, "send_email_reply", side_effect=RuntimeError("Gmail unavailable")),
        ):
            response = nodes.execute_tools({
                "user_id": "u1",
                "user_account": {"access_token": "token"},
                "approved_workflow": [step],
                "confirm_workflow": True,
            })["response"]
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["data"]["results"][0]["status"], "failed")

    def test_model_registry_configures_timeout_and_bounded_retries(self):
        get_model.cache_clear()
        with (
            patch.dict(
                os.environ,
                {
                    "GOOGLE_API_KEY": "test-key",
                    "GEMINI_FAST_MODEL": "configured-fast",
                    "GEMINI_REQUEST_TIMEOUT_SECONDS": "3",
                },
            ),
            patch("app.agent.Assistant.config.ChatGoogleGenerativeAI") as constructor,
        ):
            get_model("fast")
        constructor.assert_called_once()
        kwargs = constructor.call_args.kwargs
        self.assertEqual(kwargs["model"], "configured-fast")
        self.assertEqual(kwargs["request_timeout"], 3.0)
        self.assertEqual(kwargs["retries"], 1)
        get_model.cache_clear()


class AssistantApiTests(unittest.TestCase):
    def test_auth_loads_stored_credentials_and_closes_database_sessions(self):
        events = []

        class FakeUser:
            id = "u1"
            email = "user@example.com"
            refresh_token = "refresh"
            access_token = "******"
            token_expiry = None

        class FakeSession:
            def query(self, model):
                self.queried_model = model
                return self

            def filter(self, criterion):
                self.criterion = criterion
                return self

            def first(self):
                return FakeUser()

            def close(self):
                events.append("session_closed")

            def commit(self):
                pass

        class FakeRequest:
            def execute(self):
                events.append("google_profile")
                return {"emailAddress": "user@example.com"}

        class FakeUsers:
            def getProfile(self, **kwargs):
                self.user_id = kwargs["userId"]
                return FakeRequest()

        class FakeService:
            def users(self):
                return FakeUsers()

        def session_factory():
            events.append("session_created")
            return FakeSession()

        with (
            patch.object(
                dependencies,
                "get_gmail_service_with_token_state",
                return_value=(FakeService(), "active", None),
            ),
            patch.object(dependencies, "SessionLocal", side_effect=session_factory),
        ):
            user = dependencies.get_current_user("Bearer token")
        self.assertEqual(
            events,
            [
                "session_created",
                "session_closed",
                "google_profile",
                "session_created",
                "session_closed",
            ],
        )
        self.assertEqual(user.id, "u1")
        self.assertEqual(user.access_token, "active")

    def test_assistant_endpoint_returns_response_contract(self):
        from app.main import app

        app.dependency_overrides[dependencies.get_current_user] = lambda: AuthenticatedUser(
            id="u1",
            email="user@example.com",
            access_token="token",
            refresh_token=None,
        )
        try:
            with (
                patch("app.api.routes.assistant.assistant_graph.invoke") as invoke,
                TestClient(app) as client,
            ):
                invoke.return_value = {
                    "response": {
                        "status": "success",
                        "intent": "QUESTION",
                        "message": "Hello",
                        "data": None,
                        "actions": [],
                        "requires_confirmation": False,
                        "requires_clarification": False,
                    }
                }
                response = client.post(
                    "/api/v1/assistant/command",
                    json={"message": "Hello", "session_id": "s1"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["intent"], "QUESTION")
        finally:
            app.dependency_overrides.pop(dependencies.get_current_user, None)


class AssistantQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.temp_db.close()
        self.engine = create_engine(
            f"sqlite:///{self.temp_db.name}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.factory() as db:
            db.add_all([
                UserAccount(id="user-1", email="one@example.com"),
                UserAccount(id="user-2", email="two@example.com"),
            ])
            db.add_all([
                Email(
                    id="one-a",
                    thread_id="thread-a",
                    user_id="user-1",
                    sender="Acme <a@acme.test>",
                    subject="Quarterly report",
                    preview="Report preview",
                    body="Private body",
                    priority="HIGH",
                    category="ACTION_REQUIRED",
                    created_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
                ),
                Email(
                    id="one-b",
                    thread_id="thread-b",
                    user_id="user-1",
                    sender="Other <other@example.test>",
                    subject="Social",
                    preview="Social preview",
                    body="Private body",
                    priority="LOW",
                    category="INFORMATIONAL",
                    created_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
                ),
                Email(
                    id="two-a",
                    thread_id="thread-c",
                    user_id="user-2",
                    sender="Acme <a@acme.test>",
                    subject="Another report",
                    preview="Not for user one",
                    body="Private body",
                    priority="HIGH",
                    category="ACTION_REQUIRED",
                    created_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
                ),
            ])
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.temp_db.name)

    def test_email_search_filters_pagination_and_user_isolation(self):
        query = EmailQuerySpec(limit=1)
        first = search_emails("user-1", query, self.factory)
        second = search_emails(
            "user-1", query.model_copy(update={"offset": 1}), self.factory
        )
        self.assertEqual(first["total"], 2)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first["items"][0]["id"], "one-a")
        self.assertEqual(first["next_offset"], 1)
        self.assertIsNone(first["items"][0].get("body"))
        self.assertEqual(second["items"][0]["id"], "one-b")
        self.assertEqual(search_emails("user-2", query, self.factory)["items"][0]["id"], "two-a")

    def test_scoped_email_read_does_not_return_other_users_email(self):
        self.assertIsNone(get_email_for_user("user-1", "two-a", self.factory))
        email = get_email_for_user("user-1", "one-a", self.factory)
        self.assertEqual(email["body"], "Private body")

    def test_email_query_supports_category_and_priority_filters(self):
        query = EmailQuerySpec(
            filters=[
                EmailFilter(field="priority", operator="eq", value="HIGH"),
                EmailFilter(field="category", operator="eq", value="ACTION_REQUIRED"),
            ]
        )
        result = search_emails("user-1", query, self.factory)
        self.assertEqual([item["id"] for item in result["items"]], ["one-a"])

    def test_email_query_supports_date_ranges(self):
        query = EmailQuerySpec(
            filters=[
                EmailFilter(
                    field="received_at",
                    operator="between",
                    value=[
                        "2026-10-27T00:00:00+00:00",
                        "2026-10-29T23:59:59+00:00",
                    ],
                )
            ]
        )
        result = search_emails("user-1", query, self.factory)
        self.assertEqual([item["id"] for item in result["items"]], ["one-a"])

    def test_unread_filter_is_not_supported_by_schema(self):
        with self.assertRaises(ValueError):
            EmailFilter.model_validate({
                "field": "unread",
                "operator": "eq",
                "value": True,
            })

    def test_query_session_closes_when_database_operation_fails(self):
        class FailingSession:
            closed = False

            def query(self, model, state_model):
                raise RuntimeError(
                    f"query failed for {model.__name__} and {state_model.__name__}"
                )

            def close(self):
                self.closed = True

        session = FailingSession()
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            search_emails("user-1", EmailQuerySpec(), lambda: session)
        self.assertTrue(session.closed)

    def test_concurrent_queries_use_independent_sessions(self):
        query = EmailQuerySpec(limit=10)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda _: search_emails("user-1", query, self.factory),
                range(8),
            ))
        self.assertTrue(all(result["total"] == 2 for result in results))


if __name__ == "__main__":
    unittest.main()
