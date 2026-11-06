import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.calendar import CALENDAR_TIMEZONE

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_DATE_FORMATS = (
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B, %Y",
    "%d %b, %Y",
)


def _phrase_occurs(phrase: str, source_text: str) -> bool:
    escaped = re.escape(phrase.strip(" \t\r\n.,;:!?"))
    escaped = escaped.replace(r"\ ", r"\s+")
    return bool(
        escaped
        and re.search(
            rf"(?<!\w){escaped}(?!\w)",
            source_text,
            re.IGNORECASE,
        )
    )


class CalendarPhraseError(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _anchor_in_ist(received_at: str | None) -> datetime | None:
    if not received_at:
        return None
    try:
        parsed = datetime.fromisoformat(
            received_at[:-1] + "+00:00" if received_at.endswith("Z") else received_at
        )
    except (TypeError, ValueError):
        return None
    if parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(CALENDAR_TIMEZONE)


def email_received_at_in_ist(received_at: str | None) -> str | None:
    anchor = _anchor_in_ist(received_at)
    return anchor.isoformat() if anchor is not None else None


def _resolve_date(expression: str, anchor: datetime | None) -> date:
    phrase = expression.strip(" \t\r\n.,;:!?").casefold()
    relative_days = {
        "today": 0,
        "tomorrow": 1,
        "yesterday": -1,
        "day after tomorrow": 2,
        "the day after tomorrow": 2,
        "day before yesterday": -2,
        "the day before yesterday": -2,
        "in two days": 2,
        "in three days": 3,
    }

    if phrase in relative_days:
        if anchor is None:
            raise CalendarPhraseError(
                "invalid",
                "The email's received timestamp is unavailable, so its relative meeting date cannot be resolved.",
            )
        return anchor.date() + timedelta(days=relative_days[phrase])

    if phrase == "next week":
        raise CalendarPhraseError(
            "ambiguous",
            "Which day next week should I schedule the meeting?",
        )

    weekday_match = re.fullmatch(
        r"(next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        phrase,
    )
    if weekday_match:
        if anchor is None:
            raise CalendarPhraseError(
                "invalid",
                "The email's received timestamp is unavailable, so its relative meeting date cannot be resolved.",
            )
        period, weekday = weekday_match.groups()
        days = WEEKDAYS[weekday] - anchor.weekday()
        if period == "next" and days <= 0:
            days += 7
        return anchor.date() + timedelta(days=days)

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", phrase):
        try:
            return date.fromisoformat(phrase)
        except ValueError as error:
            raise CalendarPhraseError("invalid", "The email contains an invalid calendar date.") from error

    explicit_year = re.search(r"\b\d{4}\b", phrase) is not None
    if explicit_year:
        for date_format in _DATE_FORMATS:
            try:
                return datetime.strptime(phrase, date_format).date()
            except ValueError:
                continue
        raise CalendarPhraseError("invalid", "The email contains an invalid calendar date.")

    if anchor is None:
        raise CalendarPhraseError(
            "invalid",
            "The email's received timestamp is unavailable to determine the year for this date.",
        )

    for date_format in (
        "%B %d %Y",
        "%b %d %Y",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            parsed = datetime.strptime(f"{phrase} 2000", date_format)
            resolved = date(anchor.year, parsed.month, parsed.day)
            if resolved < anchor.date():
                raise CalendarPhraseError(
                    "ambiguous",
                    f"Which year did you mean for {expression.strip()}?",
                )
            return resolved
        except CalendarPhraseError:
            raise
        except ValueError:
            continue

    raise CalendarPhraseError(
        "invalid",
        "I couldn't validate the meeting date from the email. Please enter the date.",
    )


def _resolve_timezone(expression: str, source_text: str) -> ZoneInfo | timezone:
    value = expression.strip(" \t\r\n.,;:!?")
    if not value:
        return CALENDAR_TIMEZONE
    if not _phrase_occurs(value, source_text):
        raise CalendarPhraseError(
            "invalid",
            "The extracted timezone is not present in the email. Please confirm the meeting timezone.",
        )
    if value.casefold() in {"ist", "india standard time"}:
        return CALENDAR_TIMEZONE
    offset_match = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{2}):?(\d{2})", value, re.I)
    if offset_match:
        sign, hours, minutes = offset_match.groups()
        offset_minutes = int(hours) * 60 + int(minutes)
        if offset_minutes > 14 * 60 or int(minutes) >= 60:
            raise CalendarPhraseError("invalid", "The email contains an invalid timezone offset.")
        if sign == "-":
            offset_minutes *= -1
        return timezone(timedelta(minutes=offset_minutes))
    if value.casefold() in {"utc", "gmt"}:
        return timezone.utc
    fixed_offsets = {
        "est": -5,
        "edt": -4,
        "cst": -6,
        "cdt": -5,
        "mst": -7,
        "mdt": -6,
        "pst": -8,
        "pdt": -7,
    }
    if value.casefold() in fixed_offsets:
        return timezone(timedelta(hours=fixed_offsets[value.casefold()]))
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise CalendarPhraseError(
            "ambiguous",
            f"I couldn't identify the timezone '{value}'. Please confirm the timezone.",
        ) from error


def _parse_clock(expression: str, source_text: str) -> time:
    value = expression.strip(" \t\r\n.,;:!?")
    if not value or not _phrase_occurs(value, source_text):
        raise CalendarPhraseError(
            "invalid",
            "The extracted meeting time could not be verified in the email.",
        )
    if re.fullmatch(r"\d{1,2}(?::\d{2})?", value):
        explicit_meridiem = re.search(
            rf"(?<!\w){re.escape(value)}\s*(a\.?m\.?|p\.?m\.?)\b",
            source_text,
            re.IGNORECASE,
        )
        if explicit_meridiem:
            value = f"{value} {explicit_meridiem.group(1)}"
    match = re.fullmatch(
        r"(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?",
        value,
        re.I,
    )
    if not match:
        raise CalendarPhraseError(
            "ambiguous",
            f"I couldn't interpret '{value}' as a meeting time. Please enter the time.",
        )
    hour_text, minute_text, meridiem = match.groups()
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if minute > 59 or hour > 24 or (hour == 24 and minute != 0):
        raise CalendarPhraseError("invalid", "The email contains an invalid meeting time.")
    if meridiem:
        normalized_meridiem = meridiem.lower().replace(".", "")
        if hour < 1 or hour > 12:
            raise CalendarPhraseError("invalid", "The email contains an invalid AM/PM time.")
        hour = hour % 12 + (12 if normalized_meridiem == "pm" else 0)
    elif 1 <= hour <= 12:
        raise CalendarPhraseError(
            "ambiguous",
            f"Did you mean {hour} AM or {hour} PM?",
        )
    elif hour == 24:
        hour = 0
    return time(hour, minute)


def _find_explicit_timezone(expression: str, source_text: str) -> str:
    escaped_time = re.escape(expression.strip(" \t\r\n.,;:!?"))
    escaped_time = escaped_time.replace(r"\ ", r"\s+")
    time_match = re.search(
        rf"(?<!\w){escaped_time}(?!\w)",
        source_text,
        re.IGNORECASE,
    )
    if not time_match:
        return ""
    nearby_text = source_text[max(0, time_match.start() - 12) : time_match.end() + 48]
    timezone_match = re.search(
        r"\b(?:UTC|GMT)\s*[+-]\d{2}:?\d{2}\b"
        r"|[+-]\d{2}:\d{2}\b"
        r"|\b(?:Asia/Kolkata|[A-Za-z_]+/[A-Za-z_+-]+)\b"
        r"|\b(?:India Standard Time|UTC|GMT|IST|"
        r"EST|EDT|CST|CDT|MST|MDT|PST|PDT)\b",
        nearby_text,
        re.IGNORECASE,
    )
    return timezone_match.group(0) if timezone_match else ""


def _localize_wall_time(value: datetime, zone: ZoneInfo | timezone) -> datetime:
    if not isinstance(zone, ZoneInfo):
        return value.replace(tzinfo=zone)

    candidates: dict[timedelta | None, datetime] = {}
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == value:
            candidates[candidate.utcoffset()] = candidate
    if not candidates:
        raise CalendarPhraseError(
            "ambiguous",
            "That local time does not exist in the stated timezone. Please choose another time.",
        )
    if len(candidates) > 1:
        raise CalendarPhraseError(
            "ambiguous",
            "That local time occurs twice in the stated timezone. Please confirm which occurrence you mean.",
        )
    return next(iter(candidates.values()))


def _duration_minutes(expression: str, source_text: str) -> int | None:
    if not expression:
        return None
    if not _phrase_occurs(expression, source_text):
        raise CalendarPhraseError(
            "invalid",
            "The extracted meeting duration could not be verified in the email.",
        )
    value = expression.casefold().strip(" \t\r\n.,;:!?")
    match = re.fullmatch(
        r"(?:(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?))|half an hour",
        value,
    )
    if value == "half an hour":
        return 30
    if not match:
        raise CalendarPhraseError(
            "ambiguous",
            f"I couldn't interpret the duration '{expression}'. Please enter the duration in minutes or hours.",
        )
    amount, unit = match.groups()
    multiplier = 60 if unit.startswith(("hour", "hr")) else 1
    minutes = round(float(amount) * multiplier)
    if minutes <= 0:
        raise CalendarPhraseError("invalid", "The meeting duration must be greater than zero.")
    return minutes


def _find_explicit_duration(source_text: str) -> str:
    match = re.search(
        r"\b(?:for|lasting|duration(?:\s+of)?)\s+"
        r"(\d+(?:\.\d+)?\s*(?:minutes?|mins?|hours?|hrs?)|half an hour)\b",
        source_text,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def resolve_calendar_event_draft(
    event: dict[str, Any],
    *,
    source_text: str,
    received_at: str | None,
) -> dict[str, Any]:
    """Resolve model-extracted phrases against the email and its received time."""
    title = str(event.get("title") or "Meeting").strip() or "Meeting"
    result = {
        "title": title,
        "start_time": "",
        "end_time": "",
        "attendees": event.get("attendees") or [],
        "confidence": 0.0,
        "extraction_status": "missing",
        "ambiguity": None,
    }

    date_expression = str(event.get("date_expression") or "").strip()
    start_expression = str(event.get("start_time") or "").strip()
    end_expression = str(event.get("end_time") or "").strip()
    duration_expression = str(event.get("duration") or "").strip()
    if not duration_expression:
        duration_expression = _find_explicit_duration(source_text)

    if not date_expression:
        result["ambiguity"] = "What date should I schedule the meeting?"
        return result
    if not _phrase_occurs(date_expression, source_text):
        result.update(
            extraction_status="invalid",
            ambiguity="The extracted date does not appear in the email. Please confirm the date.",
        )
        return result

    try:
        event_date = _resolve_date(date_expression, _anchor_in_ist(received_at))
        if not start_expression:
            raise CalendarPhraseError(
                "missing",
                (
                    "What time and duration should I use?"
                    if not end_expression and not duration_expression
                    else "What time should I schedule the meeting?"
                ),
            )
        try:
            start_clock = _parse_clock(start_expression, source_text)
        except CalendarPhraseError as error:
            if (
                error.status == "ambiguous"
                and not end_expression
                and not duration_expression
            ):
                raise CalendarPhraseError(
                    "ambiguous",
                    f"{error} What duration should I use?",
                ) from error
            raise
        timezone_expression = str(event.get("timezone") or "").strip()
        if not timezone_expression:
            timezone_expression = _find_explicit_timezone(
                start_expression,
                source_text,
            )
        zone = _resolve_timezone(timezone_expression, source_text)
        start = _localize_wall_time(
            datetime.combine(event_date, start_clock),
            zone,
        )

        if end_expression:
            end_clock = _parse_clock(end_expression, source_text)
            end_date = event_date
            end = _localize_wall_time(
                datetime.combine(end_date, end_clock),
                zone,
            )
            if end <= start:
                raise CalendarPhraseError(
                    "ambiguous",
                    "The end time is not after the start time. Please confirm both times and the date.",
                )
        else:
            duration = _duration_minutes(duration_expression, source_text)
            if duration is None:
                raise CalendarPhraseError(
                    "missing",
                    "What duration should I use?",
                )
            end = (
                start.astimezone(timezone.utc) + timedelta(minutes=duration)
            ).astimezone(zone)

        if float(event.get("confidence", 0.0)) <= 0:
            raise CalendarPhraseError(
                "ambiguous",
                "Please confirm the extracted meeting details before scheduling.",
            )

    except CalendarPhraseError as error:
        result.update(
            extraction_status=error.status,
            ambiguity=str(error),
        )
        return result

    result.update(
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        confidence=float(event.get("confidence", 0.0)),
        extraction_status="complete",
    )
    return result
