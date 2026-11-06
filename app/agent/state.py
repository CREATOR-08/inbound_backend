from typing import TypedDict, List, Optional, Literal
from pydantic import BaseModel, Field

# Pydantic Schemas for Structured LLM Outputs


class TriageOutput(BaseModel):
    category: Literal["MEETING_REQUEST", "ACTION_REQUIRED", "INFORMATIONAL", "SPAM"] = Field(
        description="The primary intent classification of the email."
    )
    priority: Literal["HIGH", "MEDIUM", "LOW"] = Field(
        description="Urgency priority of the incoming email."
    )

class CalendarEventDraft(BaseModel):
    title: str = Field(description="Summary title of the meeting.")
    date_expression: str = Field(
        default="",
        description="The exact date phrase from the email, without a time, or empty if missing.",
    )
    start_time: str = Field(
        default="",
        description="The exact start clock-time phrase from the email, without a date.",
    )
    end_time: str = Field(
        default="",
        description="The exact end clock-time phrase from the email, without a date, or empty if not stated.",
    )
    duration: str = Field(
        default="",
        description="The exact duration phrase from the email, or empty if not stated.",
    )
    timezone: str = Field(
        default="",
        description="The exact explicit timezone phrase from the email, or empty if none is stated.",
    )
    attendees: List[str] = Field(default_factory=list, description="Email addresses of attendees.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence that these extracted phrases accurately reflect the email.",
    )

class ReplyDraftOutput(BaseModel):
    reply_outline: List[str] = Field(description="Key bullet points to address in response.")
    suggested_reply: str = Field(description="Polite, complete email draft ready to send.")


# LangGraph State (Shared dictionary throughout node transitions)


class EmailAgentState(TypedDict):
    email_id: str
    sender: str
    subject: str
    body: str
    
    # Classification
    category: Optional[Literal["MEETING_REQUEST", "ACTION_REQUIRED", "INFORMATIONAL", "SPAM"]]
    priority: Optional[Literal["HIGH", "MEDIUM", "LOW"]]
    
    # Generated Actions
    reply_outline: Optional[List[str]]
    suggested_reply: Optional[str]
    calendar_event: Optional[dict]
    
    # Status control
    is_archived: bool