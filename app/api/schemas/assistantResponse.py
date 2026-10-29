"""Compatibility exports for the assistant's public Pydantic schemas."""

from app.api.schemas.assistant import (
    AssistantCommandRequest,
    AssistantIntent,
    AssistantResponse,
    EmailQueryResult,
    EmailQuerySpec,
    PlannedUIAction,
)

AssistantRequest = AssistantCommandRequest
Intent = AssistantIntent
UIAction = PlannedUIAction

__all__ = [
    "AssistantCommandRequest",
    "AssistantIntent",
    "AssistantRequest",
    "AssistantResponse",
    "EmailQueryResult",
    "EmailQuerySpec",
    "Intent",
    "UIAction",
]
