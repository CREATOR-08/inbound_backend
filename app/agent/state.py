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
    start_time: str = Field(description="ISO 8601 datetime string, or empty if the date/time is ambiguous.")
    end_time: str = Field(description="ISO 8601 datetime string, or empty if the date/time is ambiguous.")
    attendees: List[str] = Field(default_factory=list, description="Email addresses of attendees.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Use 0.0 when the email does not support complete, unambiguous event times.",
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