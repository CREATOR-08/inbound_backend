"""Allow-listed, user-scoped SQLAlchemy queries used by the assistant."""

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, func
from sqlalchemy.orm import Session, aliased

from app.api.dependencies import _persist_account_token_state
from app.api.schemas.assistant import EmailFilter, EmailQuerySpec
from app.db.database import SessionLocal
from app.db.models import Email, EmailAgentStateModel, UserAccount
from app.services.gmail import (
    GmailReauthorizationRequired,
    archive_gmail_message,
    html_to_text,
)

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


def _column_for(field: str, state: Any) -> Any:
    columns = {
        "sender": Email.sender,
        "subject": Email.subject,
        "priority": func.coalesce(state.priority, Email.priority),
        "category": func.coalesce(state.category, Email.category),
        "received_at": Email.created_at,
        "is_archived": func.coalesce(state.is_archived, Email.is_archived),
    }
    return columns[field]


def _build_filter(email_filter: EmailFilter, state: Any) -> Any:
    column = _column_for(email_filter.field, state)
    value = email_filter.value
    if email_filter.field == "received_at":
        def parse(item: str) -> datetime:
            result = datetime.fromisoformat(item.replace("Z", "+00:00"))
            return (
                result.replace(tzinfo=timezone.utc)
                if result.utcoffset() is None
                else result
            )

        value = [parse(item) for item in value] if isinstance(value, list) else parse(value)

    if email_filter.operator == "eq":
        return column == value
    if email_filter.operator == "contains":
        return column.ilike(f"%{value}%")
    if email_filter.operator == "gt":
        return column > value
    if email_filter.operator == "gte":
        return column >= value
    if email_filter.operator == "lt":
        return column < value
    if email_filter.operator == "lte":
        return column <= value
    if email_filter.operator == "in":
        return column.in_(value)
    if email_filter.operator == "between":
        return column.between(value[0], value[1])
    raise ValueError(f"Unsupported filter operator: {email_filter.operator}")


def _email_payload(email: Email, state: Any | None) -> dict[str, Any]:
    return {
        "id": email.id,
        "thread_id": email.thread_id,
        "sender": email.sender,
        "subject": email.subject,
        "preview": email.preview or "",
        "received_at": email.created_at.isoformat() if email.created_at else None,
        "category": (state.category if state else None) or email.category,
        "priority": (state.priority if state else None) or email.priority,
        "is_archived": (
            state.is_archived
            if state is not None and state.is_archived is not None
            else bool(email.is_archived)
        ),
    }


def search_emails(
    user_id: str,
    query_spec: EmailQuerySpec,
    session_factory: SessionFactory = SessionLocal,
) -> dict[str, Any]:
    """Run an allow-listed, bounded email search and always close its session."""
    db = session_factory()
    try:
        state = aliased(EmailAgentStateModel)
        query = db.query(Email, state).outerjoin(state, state.email_id == Email.id)
        query = query.filter(Email.user_id == user_id)
        filters = [_build_filter(item, state) for item in query_spec.filters]
        if filters:
            query = query.filter(and_(*filters))

        sortable = {
            "received_at": Email.created_at,
            "sender": Email.sender,
            "subject": Email.subject,
            "priority": func.coalesce(state.priority, Email.priority),
        }
        order_column = sortable[query_spec.sort_by]
        query = query.order_by(
            order_column.asc() if query_spec.sort_order == "asc" else order_column.desc(),
            Email.id.asc(),
        )
        total = query.count()
        rows = query.offset(query_spec.offset).limit(query_spec.limit).all()
        next_offset = query_spec.offset + len(rows) if query_spec.offset + len(rows) < total else None
        return {
            "resource": "emails",
            "items": [_email_payload(email, state_row) for email, state_row in rows],
            "total": total,
            "limit": query_spec.limit,
            "offset": query_spec.offset,
            "next_offset": next_offset,
        }
    finally:
        db.close()


def get_email_for_user(
    user_id: str,
    email_id: str,
    session_factory: SessionFactory = SessionLocal,
) -> dict[str, Any] | None:
    """Return only the fields needed for assistant context or a reply draft."""
    db = session_factory()
    try:
        row = (
            db.query(Email, EmailAgentStateModel)
            .outerjoin(EmailAgentStateModel, EmailAgentStateModel.email_id == Email.id)
            .filter(Email.id == email_id, Email.user_id == user_id)
            .first()
        )
        if row is None:
            return None
        email, state = row
        plain_body = email.body.strip() if isinstance(email.body, str) else ""
        html_body = html_to_text(email.body_html or "") if not plain_body else ""
        body = plain_body or html_body
        body_available = bool(body)
        if not body:
            body = email.preview or ""
        return {
            **_email_payload(email, state),
            "body": body[:12_000],
            "body_available": body_available,
        }
    finally:
        db.close()


def archive_emails_for_user(user_id: str, email_ids: list[str]) -> dict[str, Any]:
    """Archive validated user-owned emails in Gmail and synchronize local state."""
    if not email_ids or len(email_ids) > 50 or len(set(email_ids)) != len(email_ids):
        raise ValueError("Archive requires 1 to 50 unique email IDs.")

    db = SessionLocal()
    try:
        emails = (
            db.query(Email)
            .filter(Email.user_id == user_id, Email.id.in_(email_ids))
            .all()
        )
        emails_by_id = {email.id: email for email in emails}
        missing_ids = [email_id for email_id in email_ids if email_id not in emails_by_id]
        if missing_ids:
            raise LookupError(
                "One or more emails were not found in the authenticated account."
            )

        account = (
            db.query(UserAccount)
            .filter(UserAccount.id == user_id)
            .first()
        )
        if account is None or not account.access_token:
            raise RuntimeError("Gmail credentials are unavailable.")

        token_state = {
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "token_expiry": account.token_expiry,
        }
        archived_ids: list[str] = []
        for index, email_id in enumerate(email_ids):
            email = emails_by_id[email_id]

            def persist_refreshed_state(
                refreshed_access: str,
                expiry: datetime | None,
                refreshed_refresh: str | None,
            ) -> None:
                persisted = _persist_account_token_state(
                    user_id,
                    refreshed_access,
                    expiry,
                    refreshed_refresh,
                    expected_access_token=token_state["access_token"],
                )
                if persisted:
                    token_state["access_token"] = refreshed_access
                    token_state["token_expiry"] = expiry
                    if refreshed_refresh is not None:
                        token_state["refresh_token"] = refreshed_refresh

            try:
                archive_gmail_message(
                    access_token=token_state["access_token"],
                    message_id=email_id,
                    refresh_token=token_state["refresh_token"],
                    token_expiry=token_state["token_expiry"],
                    on_token_refresh=persist_refreshed_state,
                )
            except Exception as error:
                db.rollback()
                logger.exception("Failed archiving email_id=%s", email_id)
                if isinstance(error, GmailReauthorizationRequired):
                    error_message = (
                        "Gmail access must be reauthorized. Reconnect your account."
                    )
                elif isinstance(error, (ValueError, LookupError)):
                    error_message = str(error)[:300]
                else:
                    error_message = "Gmail could not archive this email."
                return {
                    "archived_email_ids": archived_ids,
                    "failed_email_id": email_id,
                    "message": error_message,
                    "not_executed_email_ids": email_ids[index + 1 :],
                }

            email.is_archived = True
            if email.state is not None:
                email.state.is_archived = True
            db.commit()
            archived_ids.append(email_id)

        return {
            "archived_email_ids": archived_ids,
            "failed_email_id": None,
            "not_executed_email_ids": [],
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
