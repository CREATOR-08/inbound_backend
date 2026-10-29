from app.agent.state import EmailAgentState
from app.db.models import Email, EmailAgentStateModel
from fastapi import HTTPException
from sqlalchemy.orm import Session

__all__ = [
    "VALID_CATEGORIES",
    "VALID_PRIORITIES",
    "_get_email_action_state",
    "_build_action_state",
]

VALID_CATEGORIES = ("MEETING_REQUEST", "ACTION_REQUIRED", "INFORMATIONAL", "SPAM")


VALID_PRIORITIES = ("HIGH", "MEDIUM", "LOW")


def _get_email_action_state(
    email_id: str, user_id: str, db: Session
) -> tuple[Email, EmailAgentStateModel]:
    email = (
        db.query(Email)
        .filter(Email.id == email_id, Email.user_id == user_id)
        .first()
    )
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    state = (
        db.query(EmailAgentStateModel)
        .filter(EmailAgentStateModel.email_id == email_id)
        .first()
    )
    if not state:
        raise HTTPException(status_code=409, detail="Email has not been triaged")

    return email, state

def _build_action_state(email: Email) -> EmailAgentState:
    return {
        "email_id": email.id,
        "sender": email.sender or "",
        "subject": email.subject or "",
        "body": email.body or email.preview or "",
        "category": None,
        "priority": None,
        "reply_outline": None,
        "suggested_reply": None,
        "calendar_event": None,
        "is_archived": False,
    }
