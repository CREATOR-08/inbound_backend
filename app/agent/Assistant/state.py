"""Shared state passed between LangGraph assistant nodes."""

from typing import Any, Literal
from typing_extensions import TypedDict

Intent = Literal[
    "DATA_QUERY",
    "UI_ACTION",
    "QUESTION",
    "WORKFLOW",
    "CLARIFICATION",
]


class AssistantState(TypedDict, total=False):
    # Request identity and user input
    user_id: str
    session_id: str
    message: str
    confirm_workflow: bool
    approved_workflow: list[dict[str, Any]] | None
    query_context: dict[str, Any] | None

    # Context supplied by the application. Do not assume it is trusted.
    page_context: dict[str, Any]
    conversation_history: list[dict[str, str]]

    # Graph routing and intermediate data
    intent: Intent
    intent_reason: str
    retrieved_context: dict[str, Any]
    action_plan: list[dict[str, Any]]
    workflow_plan: list[dict[str, Any]]
    query_spec: dict[str, Any]
    execute_workflow: bool

    # Stable API response
    response: dict[str, Any]
    error: str | None
