from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel


class CalendarEventResponse(BaseModel):
  draft_id: str
  title: str
  start_time: str
  end_time: str
  status: str
  attendees: List[str]


class EmailCardResponse(BaseModel):
  id: str
  sender: str
  subject: str
  category: str
  priority: str
  reply_outline: List[str]
  suggested_reply: str
  calendar_event: Optional[CalendarEventResponse] = None


class CalendarApprovalRequest(BaseModel):
  draft_id: str
  title: Optional[str] = None
  start_time: Optional[str] = None
  end_time: Optional[str] = None
  attendees: Optional[List[str]] = None


class EmailActionRequest(BaseModel):
  email_id: str
  is_read: Optional[bool] = None


class EmailReadUpdateRequest(BaseModel):
  is_read: bool = False


class EmailReadStatusResponse(BaseModel):
  status: str = "success"
  email_id: str
  is_read: bool


class EmailSendRequest(BaseModel):
  email_id: str
  body: str


class GoogleTokenSyncRequest(BaseModel):
  email: str
  access_token: str
  access_token_expires_at: Optional[datetime] = None
  refresh_token: Optional[str] = None



class PubSubMessage(BaseModel):
    data: str
    messageId: Optional[str] = None
    publishTime: Optional[str] = None
    attributes: Optional[dict[str, str]] = None


class PubSubPushRequest(BaseModel):
    message: PubSubMessage
    subscription: Optional[str] = None


class InitialSyncResponse(BaseModel):
    status: Literal[
        "pending",
        "running",
        "completed",
        "already_completed",
        "failed",
        "not_started",
    ]
    imported_count: int
    skipped_count: int
    history_id: str
    account_status: Literal["new", "existing"] = "existing"
    sync_mode: Literal["initial", "incremental"] = "incremental"
    stored_email_count: int = 0
    incremental_sync_status: Optional[Literal["running", "completed", "failed"]] = None


class InitialSyncStatusResponse(BaseModel):
    status: Literal["not_started", "pending", "running", "completed", "failed"]
    history_id: Optional[str] = None
    account_status: Literal["new", "existing"] = "existing"
    sync_mode: Literal["initial", "incremental"] = "incremental"
    stored_email_count: int = 0
    incremental_sync_status: Optional[Literal["running", "completed", "failed"]] = None
