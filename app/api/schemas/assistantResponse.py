from typing import Any, Literal

from pydantic import BaseModel, Field


Intent = Literal[
    "UI_ACTION",
    "QUESTION",
    "WORKFLOW",
    "CLARIFICATION",
]


class AssistantRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=200)


class UIAction(BaseModel):
    type: Literal[
        "navigate",
        "filter_emails",
        "search_emails",
        "open_email",
        "show_email_summary",
        "compose_email",
        "draft_reply",
    ]

    parameters: dict[str, Any] = Field(default_factory=dict)


class AssistantResponse(BaseModel):
    message: str
    actions: list[UIAction] = Field(default_factory=list)
    requires_clarification: bool = False