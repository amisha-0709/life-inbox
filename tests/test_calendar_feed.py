"""Google Calendar import through the secret iCal address (network is faked)."""

import importlib
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

GOOD_URL = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-abc123/basic.ics"
_MODULES = ("app.main", "app.database", "app.auth", "app.config", "app.integrations.google_calendar_feed")
TODAY = datetime.now(timezone.utc).date()


def _stamp(day: date, hour: int, minute: int = 0) -> str:
    return f"{day.strftime('%Y%m%d')}T{hour:02d}{minute:02d}00Z"


def make_ics(*events: str) -> bytes:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", *events, "END:VCALENDAR"])
    return body.encode()


def vevent(uid: str, summary: str, start: str, end: str, *extra: str) -> str:
    return "\r\n".join(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{summary}", *extra, "END:VEVENT"])


def standard_feed() -> bytes:
    tomorrow = TODAY + timedelta(days=1)
    return make_ics(
        vevent("one@google.com", "Call with Riya", _stamp(tomorrow, 9, 30), _stamp(tomorrow, 10)),
        vevent("weekly@google.com", "Team standup", _stamp(TODAY + timedelta(days=2), 4), _stamp(TODAY + timedelta(days=2), 4, 15), "RRULE:FREQ=WEEKLY;COUNT=3"),
        "\r\n".join(["BEGIN:VEVENT", "UID:allday@google.com", f"DTSTART;VALUE=DATE:{tomorrow.strftime('%Y%m%d')}", "SUMMARY:Birthday", "END:VEVENT"]),
        vevent("gone@google.com", "Cancelled sync", _stamp(tomorrow, 11), _stamp(tomorrow, 12), "STATUS:CANCELLED"),
        vevent("later@google.com", "Far future", _stamp(TODAY + timedelta(days=60), 9), _stamp(TODAY + timedelta(days=60), 10)),
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cal.db'}")
    monkeypatch.setenv("LIFE_INBOX_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("LIFE_INBOX_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LIFE_INBOX_ENV", "local")
    for name in _MODULES:
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    feed_module = importlib.import_module("app.integrations.google_calendar_feed")
    state = SimpleNamespace(data=standard_feed(), calls=[], error=None)

    def fake_fetch(url):
        feed_module.validate_feed_url(url)
        state.calls.append(url)
        if state.error:
            raise feed_module.CalendarFeedError(state.error)
        return state.data

    monkeypatch.setattr(feed_module, "fetch_feed", fake_fetch)
    with TestClient(main.app) as client:
        client.headers["X-Life-Inbox-Timezone"] = "UTC"
        yield SimpleNamespace(client=client, feed=state, main=main, module=feed_module)
    importlib.import_module("app.database").engine.dispose()
    for name in _MODULES:
        sys.modules.pop(name, None)


def _calendar_tasks(client, status="all"):
    return [t for t in client.get(f"/api/tasks?status={status}").json() if t["external_source"] == "google_calendar"]


def test_connect_imports_upcoming_timed_meetings(env):
    response = env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connected"] is True
    assert body["sync"]["created"] == 3  # one call + 2 standups inside 14 days
    assert GOOD_URL not in response.text and "private-abc123" not in response.text
    assert body["calendar_hint"] == "me...@gmail.com"

    tasks = _calendar_tasks(env.client)
    titles = sorted(t["text"] for t in tasks)
    assert titles == ["Call with Riya", "Team standup", "Team standup"]
    call = next(t for t in tasks if t["text"] == "Call with Riya")
    assert call["due_on"] == (TODAY + timedelta(days=1)).isoformat()
    assert call["deadline_status"] == "calendar"
    assert call["deadline_needs_review"] is False
    assert "9:30" in call["timing_text"] and "10 AM" in call["timing_text"]


def test_feed_status_never_returns_the_secret(env):
    env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    status = env.client.get("/api/calendar/feed")
    assert status.status_code == 200
    assert status.json()["last_status"] == "ok"
    assert "private-abc123" not in status.text


@pytest.mark.parametrize("url", [
    "http://calendar.google.com/calendar/ical/me/private-x/basic.ics",
    "https://evil.example.com/calendar/ical/me/private-x/basic.ics",
    "https://calendar.google.com.evil.com/calendar/ical/x/basic.ics",
    "https://calendar.google.com/calendar/ical/me%40gmail.com/public/basic.ics",
    "https://calendar.google.com/calendar/u/0/r",
    "file:///etc/passwd",
])
def test_rejects_addresses_that_are_not_a_google_secret_ical(env, url):
    response = env.client.put("/api/calendar/feed", json={"ics_url": url})
    assert response.status_code == 400
    assert env.feed.calls == []
    assert env.client.get("/api/calendar/feed").json()["connected"] is False


def test_unreadable_feed_is_not_saved(env):
    env.feed.error = "Google did not accept this address."
    response = env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    assert response.status_code == 400
    assert "did not accept" in response.json()["detail"]
    assert env.client.get("/api/calendar/feed").json()["connected"] is False


def test_resync_is_idempotent_and_follows_moves_and_cancellations(env):
    env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    again = env.client.post("/api/calendar/feed/sync").json()["sync"]
    assert again == {"created": 0, "updated": 0, "removed": 0, "seen": 3}
    assert len(_calendar_tasks(env.client)) == 3

    moved_day = TODAY + timedelta(days=3)
    env.feed.data = make_ics(
        vevent("one@google.com", "Call with Riya (moved)", _stamp(moved_day, 15), _stamp(moved_day, 16)),
    )
    result = env.client.post("/api/calendar/feed/sync").json()["sync"]
    assert result["created"] == 0
    assert result["updated"] == 1
    assert result["removed"] == 2  # both standups disappeared
    open_tasks = _calendar_tasks(env.client, "open")
    assert [t["text"] for t in open_tasks] == ["Call with Riya (moved)"]
    assert open_tasks[0]["due_on"] == moved_day.isoformat()
    dismissed = _calendar_tasks(env.client, "dismissed")
    assert len(dismissed) == 2 and all("cancelled" in t["deadline_note"] for t in dismissed)


def test_failed_sync_changes_nothing(env):
    env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    env.feed.error = "Could not reach Google Calendar."
    response = env.client.post("/api/calendar/feed/sync")
    assert response.status_code == 502
    assert len(_calendar_tasks(env.client, "open")) == 3
    status = env.client.get("/api/calendar/feed").json()
    assert status["last_status"] == "error" and "Could not reach" in status["last_error"]


def test_user_changes_win_over_the_calendar(env):
    env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    call = next(t for t in _calendar_tasks(env.client) if t["text"] == "Call with Riya")
    my_date = (TODAY + timedelta(days=5)).isoformat()
    assert env.client.patch(f"/api/tasks/{call['id']}/deadline", json={"due_on": my_date}).status_code == 200
    assert env.client.patch(f"/api/tasks/{call['id']}", json={"text": "Prep for Riya call"}).status_code == 200
    env.client.post(f"/api/tasks/{call['id']}/complete")

    env.feed.data = make_ics()  # meeting removed from calendar
    env.client.post("/api/calendar/feed/sync")
    task = next(t for t in _calendar_tasks(env.client) if t["id"] == call["id"])
    assert task["due_on"] == my_date
    assert task["text"] == "Prep for Riya call"
    assert task["status"] != "dismissed"


def test_disconnect_keeps_imported_tasks(env):
    env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    assert env.client.delete("/api/calendar/feed").status_code == 200
    assert env.client.get("/api/calendar/feed").json()["connected"] is False
    assert len(_calendar_tasks(env.client)) == 3
    assert env.client.post("/api/calendar/feed/sync").status_code == 404
    assert env.client.delete("/api/calendar/feed").status_code == 404


def test_meeting_dates_use_the_browser_timezone(env):
    # 20:00 UTC is already the next day in India (UTC+5:30).
    late = make_ics(vevent("late@google.com", "Late call", _stamp(TODAY + timedelta(days=1), 20), _stamp(TODAY + timedelta(days=1), 21)))
    env.feed.data = late
    env.client.headers["X-Life-Inbox-Timezone"] = "Asia/Kolkata"
    env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    task = _calendar_tasks(env.client)[0]
    assert task["due_on"] == (TODAY + timedelta(days=2)).isoformat()
    assert "1:30" in task["timing_text"]


def test_background_sync_runs_for_each_owner(env):
    env.client.put("/api/calendar/feed", json={"ics_url": GOOD_URL})
    env.feed.data = make_ics(vevent("new@google.com", "New meeting", _stamp(TODAY + timedelta(days=1), 8), _stamp(TODAY + timedelta(days=1), 9)))
    env.module.sync_all_owners()
    assert "New meeting" in [t["text"] for t in _calendar_tasks(env.client, "open")]


def test_parse_feed_keeps_moved_recurring_instance_identity():
    module = importlib.import_module("app.integrations.google_calendar_feed")
    start = date(2026, 11, 16)
    data = make_ics(
        vevent("w@google.com", "Standup", _stamp(start, 9, 30), _stamp(start, 10), "RRULE:FREQ=WEEKLY;COUNT=2"),
        vevent("w@google.com", "Standup moved", _stamp(start + timedelta(days=7), 11), _stamp(start + timedelta(days=7), 11, 30), f"RECURRENCE-ID:{_stamp(start + timedelta(days=7), 9, 30)}"),
    )
    events = module.parse_feed(data, today=start, days=14, timezone_name="UTC")
    assert [e["title"] for e in events] == ["Standup", "Standup moved"]
    assert events[1]["key"] == f"w@google.com|{_stamp(start + timedelta(days=7), 9, 30)}"
    assert events[1]["due_on"] == (start + timedelta(days=7)).isoformat()
