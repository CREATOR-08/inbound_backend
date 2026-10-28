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


class EmailSendRequest(BaseModel):
  email_id: str
  body: str



class PubSubMessage(BaseModel):
    data: str
    messageId: Optional[str] = None
    publishTime: Optional[str] = None
    attributes: Optional[dict[str, str]] = None


class PubSubPushRequest(BaseModel):
    message: PubSubMessage
    subscription: Optional[str] = None


class InitialSyncResponse(BaseModel):
    status: Literal["completed", "already_completed"]
    imported_count: int
    skipped_count: int
    history_id: str
