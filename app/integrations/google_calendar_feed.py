"""Read-only Google Calendar import through the calendar's secret iCal address.

The person pastes the "Secret address in iCal format" from Google Calendar
settings. Life Inbox downloads it, and each upcoming timed meeting becomes a
task due on the meeting's day. Nothing is ever written to Google. No OAuth app
or Google Cloud project is needed. The address is a secret: it is stored only
in the local database and never returned by the API.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_EVENTS = 500
FETCH_TIMEOUT_SECONDS = 20


class CalendarFeedError(ValueError):
    """A message safe to show the person (never contains the secret URL)."""


def sync_interval_minutes() -> int:
    raw = os.getenv("LIFE_INBOX_CALENDAR_SYNC_MINUTES", "15").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 15


def import_days() -> int:
    raw = os.getenv("LIFE_INBOX_CALENDAR_DAYS", "14").strip()
    try:
        return min(90, max(1, int(raw)))
    except ValueError:
        return 14


def validate_feed_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "calendar.google.com"
        or parsed.port not in (None, 443)
        or parsed.username
        or not parsed.path.startswith("/calendar/ical/")
        or not parsed.path.endswith(".ics")
    ):
        raise CalendarFeedError(
            "Paste the “Secret address in iCal format” from Google Calendar "
            "settings. It starts with https://calendar.google.com/calendar/ical/ "
            "and ends with .ics."
        )
    if "/public/" in parsed.path:
        raise CalendarFeedError(
            "That is the public address, which only works for public calendars. "
            "Use the “Secret address in iCal format” instead."
        )
    return url


def fetch_feed(url: str) -> bytes:
    import requests

    url = validate_feed_url(url)
    try:
        with requests.get(
            url, timeout=FETCH_TIMEOUT_SECONDS, stream=True, allow_redirects=False
        ) as response:
            if response.status_code in (401, 403, 404):
                raise CalendarFeedError(
                    "Google did not accept this address. It may have been reset; "
                    "copy the secret iCal address again."
                )
            if response.status_code != 200:
                raise CalendarFeedError(
                    f"Google Calendar answered with status {response.status_code}. Try again later."
                )
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise CalendarFeedError("The calendar is too large to import (over 5 MB).")
                chunks.append(chunk)
            return b"".join(chunks)
    except CalendarFeedError:
        raise
    except requests.RequestException as exc:
        raise CalendarFeedError(
            "Could not reach Google Calendar. Check your internet connection."
        ) from exc


def _format_time(value: datetime) -> str:
    text = value.strftime("%I:%M %p").lstrip("0")
    return text.replace(":00 ", " ")


def _timing_text(start: datetime, end: Optional[datetime]) -> str:
    day = f"{start.strftime('%a')}, {start.strftime('%b')} {start.day}"
    if end and end.date() == start.date() and end > start:
        return f"{day} · {_format_time(start)}–{_format_time(end)}"
    return f"{day} · {_format_time(start)}"


def parse_feed(data: bytes, *, today: date, days: int, timezone_name: str) -> list[dict]:
    """Timed, not-cancelled meetings from ``today`` for ``days`` days, local time."""
    import icalendar
    import recurring_ical_events

    try:
        calendar = icalendar.Calendar.from_ical(data)
    except Exception as exc:  # malformed feed
        raise CalendarFeedError("That address did not return a calendar.") from exc
    if calendar.name != "VCALENDAR":
        raise CalendarFeedError("That address did not return a calendar.")

    zone = ZoneInfo(timezone_name)
    recurring_uids = {
        str(component.get("UID"))
        for component in calendar.walk("VEVENT")
        if component.get("RRULE") is not None or component.get("RECURRENCE-ID") is not None
    }
    window_end = today + timedelta(days=days)
    occurrences = recurring_ical_events.of(calendar).between(
        today - timedelta(days=1), window_end + timedelta(days=1)
    )

    events = []
    for event in occurrences:
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = event.decoded("DTSTART")
        if not isinstance(start, datetime):
            continue  # all-day: birthdays, holidays, out-of-office, not meetings
        if start.tzinfo is None:
            start = start.replace(tzinfo=zone)
        start = start.astimezone(zone)
        if not (today <= start.date() < window_end):
            continue
        end = None
        if event.get("DTEND") is not None:
            end_value = event.decoded("DTEND")
            if isinstance(end_value, datetime):
                end = (end_value if end_value.tzinfo else end_value.replace(tzinfo=zone)).astimezone(zone)
        uid = str(event.get("UID") or "")
        if not uid:
            continue
        key = uid
        if uid in recurring_uids:
            recurrence = event.get("RECURRENCE-ID")
            original = recurrence.dt if recurrence is not None else event.decoded("DTSTART")
            if isinstance(original, datetime):
                original = (original if original.tzinfo else original.replace(tzinfo=zone)).astimezone(ZoneInfo("UTC"))
                key = f"{uid}|{original.strftime('%Y%m%dT%H%M%SZ')}"
            else:
                key = f"{uid}|{original.isoformat()}"
        title = " ".join(str(event.get("SUMMARY") or "").split()) or "Busy"
        events.append(
            {
                "key": key,
                "title": title[:300],
                "start_at": start.isoformat(),
                "due_on": start.date().isoformat(),
                "timing_text": _timing_text(start, end),
            }
        )
        if len(events) >= MAX_EVENTS:
            break
    return events


def sync_current_owner(*, today: Optional[date] = None) -> dict:
    """Import meetings for the owner bound to this request or thread."""
    from app import database

    source = database.get_calendar_feed_source()
    if source is None:
        raise CalendarFeedError("No Google Calendar is connected.")
    url, timezone_name = source
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        timezone_name, zone = "UTC", ZoneInfo("UTC")
    today = today or datetime.now(zone).date()
    days = import_days()
    try:
        events = parse_feed(fetch_feed(url), today=today, days=days, timezone_name=timezone_name)
    except CalendarFeedError as exc:
        # On failure nothing is changed: no meeting is dismissed.
        database.record_calendar_sync(status="error", error=str(exc), imported=0)
        raise
    result = database.apply_calendar_events(
        events,
        window_start=today.isoformat(),
        window_end=(today + timedelta(days=days)).isoformat(),
        timezone_name=timezone_name,
    )
    feed = database.record_calendar_sync(status="ok", error=None, imported=result["seen"])
    return {**result, "feed": feed, "days": days}


def sync_all_owners() -> None:
    """Background job: sync each owner's feed under that owner's identity."""
    from app import database

    for owner_id in database.list_calendar_feed_owner_ids():
        database.set_current_owner_id(owner_id)
        try:
            sync_current_owner()
        except CalendarFeedError as exc:
            logger.info("Calendar sync for owner %s skipped: %s", owner_id, exc)
        except Exception:
            logger.exception("Calendar sync failed for owner %s", owner_id)
        finally:
            database.set_current_owner_id(None)
