"""Validated API and query schemas for the assistant."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AssistantIntent = Literal[
    "DATA_QUERY",
    "UI_ACTION",
    "QUESTION",
    "WORKFLOW",
    "CLARIFICATION",
]
QueryField = Literal[
    "sender",
    "subject",
    "priority",
    "category",
    "received_at",
    "is_archived",
]
QueryOperator = Literal[
    "eq",
    "contains",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "between",
]


class EmailFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: QueryField
    operator: QueryOperator
    value: str | bool | list[str] | list[bool]

    @model_validator(mode="after")
    def validate_field_operator_value(self) -> "EmailFilter":
        if isinstance(self.value, str) and len(self.value) > 500:
            raise ValueError("Filter values cannot exceed 500 characters.")
        if isinstance(self.value, list) and (
            not self.value or len(self.value) > 20
        ):
            raise ValueError("Filter lists must contain between 1 and 20 values.")
        string_fields = {"sender", "subject", "priority", "category"}
        if self.field in string_fields:
            if self.operator not in {"eq", "contains", "in"}:
                raise ValueError(f"{self.operator} is not valid for {self.field}.")
            if self.field in {"priority", "category"} and self.operator == "contains":
                raise ValueError(f"contains is not valid for {self.field}.")
            values = self.value if isinstance(self.value, list) else [self.value]
            if self.operator == "contains" and not isinstance(self.value, str):
                raise ValueError("'contains' requires a single string value.")
            if not all(isinstance(value, str) and value for value in values):
                raise ValueError(f"{self.field} filters require non-empty strings.")
            if self.field == "priority" and any(
                value.upper() not in {"HIGH", "MEDIUM", "LOW"} for value in values
            ):
                raise ValueError("priority must be HIGH, MEDIUM, or LOW.")
            if self.field == "category" and any(
                value.upper() not in {
                    "MEETING_REQUEST",
                    "ACTION_REQUIRED",
                    "INFORMATIONAL",
                    "SPAM",
                }
                for value in values
            ):
                raise ValueError("category is not supported.")
            if self.field in {"priority", "category"}:
                normalized = (
                    [value.upper() for value in self.value]
                    if isinstance(self.value, list)
                    else self.value.upper()
                )
                object.__setattr__(self, "value", normalized)
            if self.operator == "in" and not isinstance(self.value, list):
                raise ValueError("The in operator requires a list of values.")
        elif self.field == "is_archived":
            if self.operator not in {"eq", "in"}:
                raise ValueError("is_archived supports only eq and in.")
            values = self.value if isinstance(self.value, list) else [self.value]
            if not all(isinstance(value, bool) for value in values):
                raise ValueError("is_archived filters require boolean values.")
        else:
            if self.operator not in {"eq", "gt", "gte", "lt", "lte", "between"}:
                raise ValueError("received_at supports date comparisons only.")
            values = self.value if isinstance(self.value, list) else [self.value]
            if not all(isinstance(value, str) for value in values):
                raise ValueError("received_at filters require ISO date strings.")
            try:
                parsed_values = [
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                    for value in values
                ]
            except ValueError as error:
                raise ValueError("received_at values must be ISO 8601 dates.") from error
            if self.operator == "between" and len(values) != 2:
                raise ValueError("between requires exactly two dates.")
            if self.operator == "between":
                start, end = parsed_values
                if start.utcoffset() is None:
                    start = start.replace(tzinfo=timezone.utc)
                if end.utcoffset() is None:
                    end = end.replace(tzinfo=timezone.utc)
                if end < start:
                    raise ValueError("between requires start date before end date.")
            if self.operator != "between" and isinstance(self.value, list):
                raise ValueError("Date comparisons accept one date value.")
        return self


class EmailQuerySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: Literal["emails"] = "emails"
    filters: list[EmailFilter] = Field(default_factory=list, max_length=10)
    sort_by: Literal["received_at", "sender", "subject", "priority"] = "received_at"
    sort_order: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10_000)


class PlannedUIAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "NAVIGATE",
        "OPEN_EMAIL",
        "OPEN_COMPOSE",
        "FILTER_EMAILS",
        "CLEAR_FILTERS",
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(max_length=300)

    @model_validator(mode="after")
    def validate_parameters(self) -> "PlannedUIAction":
        if self.type == "NAVIGATE":
            if set(self.parameters) != {"page"} or self.parameters["page"] not in {
                "inbox", "sent", "drafts", "calendar",
            }:
                raise ValueError("NAVIGATE requires a supported page.")
        elif self.type == "OPEN_EMAIL":
            if set(self.parameters) != {"email_id"} or not isinstance(
                self.parameters["email_id"], str
            ):
                raise ValueError("OPEN_EMAIL requires an email_id.")
        elif self.type == "FILTER_EMAILS":
            if set(self.parameters) != {"filter"} or self.parameters["filter"] not in {
                "ALL", "URGENT",
            }:
                raise ValueError("FILTER_EMAILS requires a supported filter.")
        elif self.type == "OPEN_COMPOSE" and self.parameters:
            if set(self.parameters) != {"to", "subject", "body"} or not all(
                isinstance(self.parameters[key], str)
                for key in ("to", "subject", "body")
            ):
                raise ValueError(
                    "OPEN_COMPOSE accepts only string to, subject, and body values."
                )
            if any(
                len(self.parameters[key]) > limit
                for key, limit in (("to", 500), ("subject", 500), ("body", 12_000))
            ):
                raise ValueError("OPEN_COMPOSE values exceed their size limits.")
        elif self.parameters:
            raise ValueError(f"{self.type} does not accept parameters.")
        return self


WorkflowTool = Literal[
    "search_emails",
    "read_email",
    "draft_email",
    "send_email_reply",
    "unknown",
]


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: WorkflowTool
    parameters: dict[str, Any] = Field(default_factory=dict, max_length=10)
    description: str = Field(max_length=300)

    @model_validator(mode="after")
    def validate_tool_parameters(self) -> "WorkflowStep":
        if self.tool == "search_emails":
            if set(self.parameters) != {"query"}:
                raise ValueError("search_emails requires only a query specification.")
            EmailQuerySpec.model_validate(self.parameters["query"])
        elif self.tool in {"read_email", "draft_email"}:
            if set(self.parameters) - {"email_id"}:
                raise ValueError(f"{self.tool} accepts only email_id.")
            email_id = self.parameters.get("email_id")
            if email_id is not None and (
                not isinstance(email_id, str) or not email_id or len(email_id) > 500
            ):
                raise ValueError(f"{self.tool} requires a valid email_id.")
        elif self.tool == "send_email_reply":
            if set(self.parameters) - {"email_id", "body"}:
                raise ValueError("send_email_reply accepts only email_id and body.")
            email_id = self.parameters.get("email_id")
            body = self.parameters.get("body")
            if email_id is not None and (
                not isinstance(email_id, str) or not email_id or len(email_id) > 500
            ):
                raise ValueError("send_email_reply requires a valid email_id.")
            if (
                not isinstance(body, str) or not body.strip() or len(body) > 12_000
            ):
                raise ValueError("send_email_reply requires a valid reply body.")
        elif self.parameters:
            raise ValueError("unknown does not accept parameters.")
        return self


class EmailSummary(BaseModel):
    id: str
    thread_id: str | None
    sender: str
    subject: str
    preview: str
    received_at: str | None
    category: str | None
    priority: str | None
    is_archived: bool


class EmailQueryResult(BaseModel):
    resource: Literal["emails"]
    items: list[EmailSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=50)
    offset: int = Field(ge=0, le=10_000)
    next_offset: int | None = Field(default=None, ge=0, le=10_000)
    query_spec: EmailQuerySpec


class AssistantCommandRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    session_id: str = Field(min_length=1, max_length=200)
    page_context: dict[str, Any] = Field(default_factory=dict)
    conversation_history: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    query_context: EmailQuerySpec | None = None
    approved_workflow: list[WorkflowStep] | None = Field(default=None, max_length=10)
    confirm_workflow: bool = False


class AssistantResponse(BaseModel):
    status: Literal[
        "success",
        "error",
        "invalid_request",
        "awaiting_confirmation",
        "confirmed",
        "rate_limited",
        "unsupported",
    ] = "success"
    intent: AssistantIntent
    message: str
    data: EmailQueryResult | dict[str, Any] | None = None
    actions: list[PlannedUIAction] = Field(default_factory=list)
    requires_confirmation: bool = False
    requires_clarification: bool = False
