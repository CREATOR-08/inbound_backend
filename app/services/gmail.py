import base64
import binascii
import html
import logging
import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.api.google_profile_cache import invalidate_google_profile_verification

logger = logging.getLogger(__name__)

GMAIL_PUBSUB_TOPIC = os.getenv(
    "GMAIL_PUBSUB_TOPIC", "projects/inbound-511714/topics/gmail-notifications"
)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailReauthorizationRequired(Exception):
    """Raised when an expired Gmail token cannot be refreshed."""


class GmailHistoryCursorExpired(RuntimeError):
    """Raised when Gmail no longer retains the requested history cursor."""


def _invalidate_profile_cache_after_google_error(
    error: HttpError,
    *access_tokens: str,
) -> None:
    status = getattr(getattr(error, "resp", None), "status", None)
    if status in (401, 403):
        for access_token in access_tokens:
            invalidate_google_profile_verification(access_token)


TokenRefreshCallback = Callable[
    [str, Optional[datetime], Optional[str]], None
]


def get_gmail_service_with_token_state(
    access_token: str,
    refresh_token: Optional[str] = None,
    token_expiry: Optional[datetime] = None,
    *,
    refresh_if_expiry_missing: bool = False,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
) -> Tuple[Any, str, Optional[datetime]]:
    """Build a Gmail client and return any refreshed token state."""
    normalized_refresh_token = refresh_token or None
    credentials_expiry = token_expiry
    if credentials_expiry and credentials_expiry.tzinfo:
        credentials_expiry = credentials_expiry.astimezone(
            timezone.utc
        ).replace(tzinfo=None)
    credentials = Credentials(
        token=access_token,
        refresh_token=normalized_refresh_token,
        token_uri=GMAIL_TOKEN_URI,
        client_id=GOOGLE_CLIENT_ID or None,
        client_secret=GOOGLE_CLIENT_SECRET or None,
        expiry=credentials_expiry,
    )

    should_refresh = credentials.expired or (
        refresh_if_expiry_missing and token_expiry is None
    )
    if should_refresh:
        if not normalized_refresh_token:
            raise GmailReauthorizationRequired(
                "Reconnect your Google account to restore Gmail synchronization."
            )
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth client credentials are not configured.")

        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            logger.warning(
                "Google OAuth token refresh failed (%s)",
                type(exc).__name__,
            )
            raise GmailReauthorizationRequired(
                "Reconnect your Google account to restore Gmail synchronization."
            ) from exc
        if on_token_refresh:
            refreshed_expiry = credentials.expiry
            if refreshed_expiry and refreshed_expiry.tzinfo is None:
                refreshed_expiry = refreshed_expiry.replace(tzinfo=timezone.utc)
            elif refreshed_expiry:
                refreshed_expiry = refreshed_expiry.astimezone(timezone.utc)
            on_token_refresh(
                credentials.token,
                refreshed_expiry,
                credentials.refresh_token,
            )

    service = build("gmail", "v1", credentials=credentials)
    refreshed_expiry = credentials.expiry
    if refreshed_expiry and refreshed_expiry.tzinfo is None:
        refreshed_expiry = refreshed_expiry.replace(tzinfo=timezone.utc)
    elif refreshed_expiry:
        refreshed_expiry = refreshed_expiry.astimezone(timezone.utc)
    return service, credentials.token or access_token, refreshed_expiry


def get_gmail_service_from_token(
    access_token: str,
    refresh_token: Optional[str] = None,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
) -> Tuple[Any, str]:
    """Builds a Gmail API client dynamically using the OAuth token.

    Automatically refreshes expired access tokens if a refresh token is
    available. Returns (service, valid_access_token).
    """
    service, active_token, _ = get_gmail_service_with_token_state(
        access_token,
        refresh_token,
        token_expiry,
        on_token_refresh=on_token_refresh,
    )
    return service, active_token


def _decode_base64_payload(data: str) -> str:
    """Safely decodes Gmail URL-safe base64 data to UTF-8 text."""
    if not data:
        return ""
    try:
        padded_data = data + "=" * (-len(data) % 4)
        decoded = base64.b64decode(
            padded_data.encode("ASCII"),
            altchars=b"-_",
            validate=True,
        )
        return decoded.decode(
            "utf-8", errors="replace"
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError("Gmail returned a malformed encoded message body.") from error


class _EmailHTMLTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
        "footer", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol",
        "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _EmailHTMLTextParser()
    parser.feed(value)
    parser.close()
    lines = (part.strip() for part in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def clean_text_for_llm(raw_text: str) -> str:
    """Strips massive URLs, footers, and redundant lines to preserve LLM context window."""
    if not raw_text:
        return ""

    text = raw_text
    footer_delimiters = [
        "----------------------------------------",
        "This email was intended for",
        "You are receiving",
        "Unsubscribe:",
        "© 202",
    ]
    for delimiter in footer_delimiters:
        if delimiter in text:
            text = text.split(delimiter)[0]

    lines = text.splitlines()
    quote_markers = (
        re.compile(r"^\s*On .+ wrote:\s*$", re.IGNORECASE),
        re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
        re.compile(r"^\s*From:\s+\S+", re.IGNORECASE),
        re.compile(r"^\s*--\s*$"),
    )
    meeting_cue_before_quote = re.compile(
        r"\b(meet|meeting|appointment|schedule)\b",
        re.IGNORECASE,
    )
    date_cue_before_quote = re.compile(
        r"\b(today|tomorrow|yesterday|day after tomorrow|day before yesterday|"
        r"next week|next monday|next tuesday|next wednesday|next thursday|"
        r"next friday|next saturday|next sunday|this monday|this tuesday|"
        r"this wednesday|this thursday|this friday|this saturday|this sunday|"
        r"january|february|march|april|may|june|july|august|september|"
        r"october|november|december|monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday)\b",
        re.IGNORECASE,
    )
    clock_cue_before_quote = re.compile(
        r"\b(?:at\s*)?\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?\b",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        if any(marker.match(line) for marker in quote_markers):
            preceding = "\n".join(lines[:index])
            if (
                meeting_cue_before_quote.search(preceding)
                and date_cue_before_quote.search(preceding)
                and clock_cue_before_quote.search(preceding)
            ):
                lines = lines[:index]
                break
    text = "\n".join(lines)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _extract_bodies_from_payload(payload: Dict[str, Any]) -> Tuple[str, str]:
    """Recursively traverses the MIME structure to extract both plain text and HTML bodies."""
    plain_parts: List[str] = []
    html_parts: List[str] = []
    decode_errors: list[ValueError] = []

    def walk_parts(part: Dict[str, Any]) -> None:
        if part.get("filename"):
            return
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data", "")

        if body_data:
            try:
                decoded = _decode_base64_payload(body_data)
            except ValueError as error:
                decode_errors.append(error)
            else:
                if mime_type == "text/plain":
                    plain_parts.append(decoded)
                elif mime_type == "text/html":
                    html_parts.append(decoded)

        for sub_part in part.get("parts", []):
            walk_parts(sub_part)

    walk_parts(payload)
    body_plain = "\n".join(plain_parts).strip()
    body_html = "\n".join(html_parts).strip()
    if not body_plain and not body_html and decode_errors:
        raise ValueError("Gmail message body could not be decoded.") from decode_errors[0]
    if decode_errors:
        logger.warning(
            "Skipped %s malformed MIME body part(s)",
            len(decode_errors),
        )
    body_html = "\n".join(html_parts).strip()

    if not body_plain and body_html:
        body_plain = html_to_text(body_html)
    if not body_html and body_plain:
        body_html = (
            "<pre style='white-space: pre-wrap; font-family: inherit;'>"
            f"{html.escape(body_plain)}</pre>"
        )

    return body_plain, body_html


# =========================================================================
# Real-Time Pub/Sub & Delta Sync Helpers
# =========================================================================


def start_gmail_watch(
    access_token: str,
    refresh_token: Optional[str] = None,
    topic_name: str = GMAIL_PUBSUB_TOPIC,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
) -> Dict[str, Any]:
    """Registers Gmail to push INBOX updates to the Cloud Pub/Sub topic."""
    service, active_token = get_gmail_service_from_token(
        access_token, refresh_token, token_expiry, on_token_refresh
    )
    body = {
        "labelIds": ["INBOX"],
        "topicName": topic_name,
    }
    try:
        return service.users().watch(userId="me", body=body).execute()
    except HttpError as exc:
        _invalidate_profile_cache_after_google_error(
            exc, access_token, active_token
        )
        raise


def stop_gmail_watch(
    access_token: str,
    refresh_token: Optional[str] = None,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
) -> None:
    """Stops Gmail push notifications for the authenticated mailbox."""
    service, active_token = get_gmail_service_from_token(
        access_token, refresh_token, token_expiry, on_token_refresh
    )
    try:
        service.users().stop(userId="me").execute()
    except HttpError as exc:
        _invalidate_profile_cache_after_google_error(
            exc, access_token, active_token
        )
        raise


def fetch_new_message_ids_from_history(
    access_token: str,
    start_history_id: str,
    refresh_token: Optional[str] = None,
    raise_on_expired: bool = False,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
    history_types: Optional[List[str]] = None,
) -> Tuple[List[str], str, str]:
    """Queries Gmail History API for all messages added to INBOX since start_history_id.

    Returns: (new_message_ids, latest_history_id, active_token)
    """
    service, active_token = get_gmail_service_from_token(
        access_token, refresh_token, token_expiry, on_token_refresh
    )
    new_ids: set[str] = set()
    latest_history_id = start_history_id
    page_token = None

    try:
        while True:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=history_types or ["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )

            latest_history_id = resp.get("historyId", latest_history_id)

            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    message_id = msg.get("id")
                    message_labels = msg.get("labelIds")
                    if message_id and (
                        not isinstance(message_labels, list)
                        or "INBOX" in message_labels
                    ):
                        new_ids.add(message_id)
                for change_key in ("labelsAdded", "labelsRemoved"):
                    for change in record.get(change_key, []):
                        msg = change.get("message", {})
                        if msg.get("id"):
                            new_ids.add(msg["id"])

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        if isinstance(e, HttpError):
            _invalidate_profile_cache_after_google_error(
                e, access_token, active_token
            )
            status = getattr(getattr(e, "resp", None), "status", None)
            if status == 401 and not refresh_token:
                raise GmailReauthorizationRequired(
                    "Reconnect your Google account to restore Gmail synchronization."
                ) from e
        # History IDs expire after ~1-4 weeks; 404 indicates history window lapsed
        if "History id not found" in str(e) or "404" in str(e):
            logger.warning(
                "History ID %s expired. Returning empty set.", start_history_id
            )
            if raise_on_expired:
                raise GmailHistoryCursorExpired(
                    f"Gmail history cursor {start_history_id} expired"
                ) from e
            return [], start_history_id, active_token
        raise

    return list(new_ids), str(latest_history_id), active_token


def list_message_ids(
    access_token: str,
    query: str,
    refresh_token: Optional[str] = None,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
) -> Tuple[List[str], str]:
    """Lists every Gmail message matching a query, following all result pages."""
    service, active_token = get_gmail_service_from_token(
        access_token, refresh_token, token_expiry, on_token_refresh
    )
    message_ids: List[str] = []
    page_token = None

    while True:
        try:
            response = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=500,
                    pageToken=page_token,
                )
                .execute()
            )
        except HttpError as exc:
            _invalidate_profile_cache_after_google_error(
                exc, access_token, active_token
            )
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 401 and not refresh_token:
                raise GmailReauthorizationRequired(
                    "Reconnect your Google account to restore Gmail synchronization."
                ) from exc
            raise
        message_ids.extend(
            message["id"]
            for message in response.get("messages", [])
            if message.get("id")
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return list(dict.fromkeys(message_ids)), active_token


def fetch_single_email(
    access_token: str,
    message_id: str,
    refresh_token: Optional[str] = None,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
    num_retries: int = 0,
) -> Dict[str, Any]:
    """Fetches full payload, headers, and parsed body content for a single message."""
    service, active_token = get_gmail_service_from_token(
        access_token, refresh_token, token_expiry, on_token_refresh
    )
    try:
        request = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
        )
        message = (
            request.execute(num_retries=num_retries)
            if num_retries
            else request.execute()
        )
    except HttpError as exc:
        _invalidate_profile_cache_after_google_error(
            exc, access_token, active_token
        )
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 401 and not refresh_token:
            raise GmailReauthorizationRequired(
                "Reconnect your Google account to restore Gmail synchronization."
            ) from exc
        raise

    payload = message.get("payload", {})
    headers = {
        h["name"].lower(): h["value"] for h in payload.get("headers", [])
    }

    try:
        body_plain, body_html = _extract_bodies_from_payload(payload)
    except ValueError:
        logger.warning(
            "Unable to decode message body for Gmail message %s",
            message_id,
            exc_info=True,
        )
        body_plain, body_html = "", ""

    received_at = None
    if message.get("internalDate"):
        received_at = datetime.fromtimestamp(
            int(message["internalDate"]) / 1000, tz=timezone.utc
        )
    elif headers.get("date"):
        try:
            received_at = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError, OverflowError):
            received_at = datetime.now(timezone.utc)

    labels = message.get("labelIds")
    if not isinstance(labels, list):
        raise ValueError("Gmail message response is missing labelIds.")
    is_read = "UNREAD" not in labels

    return {
        "id": message["id"],
        "thread_id": message.get("threadId", message["id"]),
        "sender": headers.get("from", "Unknown"),
        "subject": headers.get("subject", "(No Subject)"),
        "preview": message.get("snippet", ""),
        "body": body_plain,
        "body_html": body_html,
        "has_html": bool(body_html),
        "received_at": received_at,
        "access_token": active_token,
        "is_read": is_read,
        "label_ids": labels,
    }


# =========================================================================
# Read/Unread Status Operations
# =========================================================================


def update_gmail_message_read_status(
    access_token: str,
    message_id: str,
    is_read: bool,
    refresh_token: Optional[str] = None,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
) -> Dict[str, Any]:
    """Synchronize the UNREAD label in Gmail for a message."""
    service, active_token = get_gmail_service_from_token(
        access_token, refresh_token, token_expiry, on_token_refresh
    )
    body: Dict[str, Any] = {}
    if is_read:
        body["removeLabelIds"] = ["UNREAD"]
    else:
        body["addLabelIds"] = ["UNREAD"]

    try:
        return (
            service.users()
            .messages()
            .modify(userId="me", id=message_id, body=body)
            .execute()
        )
    except HttpError as exc:
        _invalidate_profile_cache_after_google_error(
            exc, access_token, active_token
        )
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 401 and not refresh_token:
            raise GmailReauthorizationRequired(
                "Reconnect your Google account to restore Gmail synchronization."
            ) from exc
        raise


# =========================================================================
# Reply Operations
# =========================================================================


def send_email_reply(
    access_token: str,
    email_id: str,
    thread_id: str,
    sender: str,
    subject: str,
    body: str,
    refresh_token: Optional[str] = None,
    token_expiry: Optional[datetime] = None,
    on_token_refresh: Optional[TokenRefreshCallback] = None,
) -> str:
    """Send a reply through Gmail and return the created message ID."""
    recipient = parseaddr(sender)[1]
    if not recipient or "@" not in recipient:
        raise ValueError("The sender address is not a valid reply recipient")
    if not body.strip():
        raise ValueError("Reply body cannot be empty")

    service, active_token = get_gmail_service_from_token(
        access_token, refresh_token, token_expiry, on_token_refresh
    )
    try:
        original = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=email_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References"],
            )
            .execute()
        )
    except HttpError as exc:
        _invalidate_profile_cache_after_google_error(
            exc, access_token, active_token
        )
        raise
    headers = {
        h["name"].lower(): h["value"]
        for h in original.get("payload", {}).get("headers", [])
    }
    message_id = headers.get("message-id")

    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = (
        subject if subject.lower().startswith("re:") else f"Re: {subject}"
    )
    if message_id:
        message["In-Reply-To"] = message_id
        references = headers.get("references", "")
        message["References"] = f"{references} {message_id}".strip()
    message.set_content(body)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": thread_id})
            .execute()
        )
    except HttpError as exc:
        _invalidate_profile_cache_after_google_error(
            exc, access_token, active_token
        )
        raise
    return sent["id"]