import uuid
from sqlalchemy import Column, String, Text, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, synonym
from sqlalchemy.sql import func
from app.db.database import Base


class UserAccount(Base):
    __tablename__ = "user_accounts"

    # User ID matching your Better Auth or Google OAuth user subject ID
    id = Column(String, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime(timezone=True), nullable=True)

    # Gmail Pub/Sub sync state
    last_history_id = Column(String(64), nullable=True)
    watch_expiration = Column(String(64), nullable=True)
    initial_sync_completed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), default=func.now())

    emails = relationship("Email", back_populates="user", cascade="all, delete-orphan")


class Email(Base):
    __tablename__ = "emails"

    id = Column(String, primary_key=True, index=True)
    thread_id = Column(String, index=True, nullable=True)
    user_id = Column(String, ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=True, index=True)
    sender = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    preview = Column(Text, nullable=True)
    body = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    has_html = Column(Boolean, default=False)
    category = Column(String, nullable=True)
    priority = Column(String, nullable=True)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    received_at = synonym("created_at")

    user = relationship("UserAccount", back_populates="emails")

    state = relationship(
        "EmailAgentStateModel",
        back_populates="email",
        uselist=False,
        cascade="all, delete-orphan",
    )
    calendar_draft = relationship(
        "CalendarDraft",
        back_populates="email",
        uselist=False,
        cascade="all, delete-orphan",
    )


class EmailAgentStateModel(Base):
    __tablename__ = "email_agent_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_id = Column(String(64), ForeignKey("emails.id", ondelete="CASCADE"), unique=True, nullable=False)
    category = Column(String(32), nullable=True)
    priority = Column(String(16), nullable=True)
    reply_outline = Column(JSON, nullable=True)   # Stores outline bullet points
    suggested_reply = Column(Text, nullable=True)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    email = relationship("Email", back_populates="state")


class CalendarDraft(Base):
    __tablename__ = "calendar_drafts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_id = Column(String(64), ForeignKey("emails.id", ondelete="CASCADE"), unique=True, nullable=False)
    title = Column(String(255), nullable=False)
    start_time = Column(String(64), nullable=False)
    end_time = Column(String(64), nullable=False)
    attendees = Column(JSON, default=list)
    confidence = Column(String(16), nullable=True)
    status = Column(String(32), default="PENDING")   # PENDING, APPROVED, CANCELLED
    google_event_id = Column(String(128), nullable=True)

    email = relationship("Email", back_populates="calendar_draft")