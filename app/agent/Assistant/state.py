from typing import Any, Literal
from typing_extensions import TypedDict


class AssistantState(TypedDict, total=False):
    # Request information
    user_id: str
    session_id: str
    message: str

    # Lightweight session context
    page_context: dict[str, Any]
    conversation_history: list[dict[str, str]]

    # Routing
    intent: Literal[
        "UI_ACTION",
        "QUESTION",
        "WORKFLOW",
        "CLARIFICATION",
    ]

    # Information retrieved when needed
    retrieved_context: dict[str, Any]

    # Agent output
    action_plan: list[dict[str, Any]]
    response: dict[str, Any]

    # Error handling
    error: str | None