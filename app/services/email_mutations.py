"""User-scoped email state mutations shared by API and assistant workflows."""

from sqlalchemy.orm import Session

from app.api.dependencies import _persist_account_token_state
from app.db.models import Email, UserAccount
from app.services.gmail import update_gmail_message_read_status


def set_email_read_status(
    email: Email,
    is_read: bool,
    user: UserAccount,
    token: str,
    db: Session,
) -> Email:
    """Update local and Gmail read state, restoring local state if Gmail fails."""
    original_is_read = bool(email.is_read)
    email.is_read = is_read
    db.commit()
    try:
        update_gmail_message_read_status(
            access_token=token,
            message_id=email.id,
            is_read=is_read,
            refresh_token=user.refresh_token,
            token_expiry=user.token_expiry,
            on_token_refresh=lambda refreshed_access, expiry, refreshed_refresh: (
                _persist_account_token_state(
                    user.id,
                    refreshed_access,
                    expiry,
                    refreshed_refresh,
                    expected_access_token=token,
                )
            ),
        )
    except Exception:
        email.is_read = original_is_read
        db.commit()
        raise
    return email
