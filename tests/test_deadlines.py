"""Deadlines keep their source-time meaning across the complete local workflow."""

from contextlib import ExitStack
from datetime import datetime, timezone
import importlib
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tests.pdf_fixtures import make_text_pdf
from tests.analysis_fixtures import mock_analysis


_MODULES = (
    "app.main", "app.database", "app.auth", "app.config",
    "app.integrations.calendar", "app.retrieval.ask",
)


def _purge_app_modules():
    for name in _MODULES:
        sys.modules.pop(name, None)


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    database_path = tmp_path / "deadlines.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("LIFE_INBOX_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("LIFE_INBOX_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LIFE_INBOX_ENV", "local")
    monkeypatch.setenv("LIFE_INBOX_CALENDAR_PROVIDER", "mock")
    clock = SimpleNamespace(instant=datetime(2026, 10, 15, 10, tzinfo=timezone.utc))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return clock.instant.replace(tzinfo=None)
            return clock.instant.astimezone(tz)

    with ExitStack() as stack:
        engines = []

        def start():
            _purge_app_modules()
            main = importlib.import_module("app.main")
            database = importlib.import_module("app.database")
            dates = importlib.import_module("app.attention.dates")
            monkeypatch.setattr(database, "datetime", Clock)
            monkeypatch.setattr(dates, "datetime", Clock)
            assert database.engine.url.database == str(database_path)
            engines.append(database.engine)
            client = stack.enter_context(TestClient(main.app))
            return SimpleNamespace(client=client, main=main, db=database)

        env = start()
        env.clock = clock
        env.restart = start
        yield env
    for engine in engines:
        engine.dispose()
    _purge_app_modules()


def at(inbox, timestamp):
    inbox.clock.instant = datetime.fromisoformat(timestamp)


def capture(inbox, text="Pay the electricity bill today", zone="Asia/Kolkata"):
    response = inbox.client.post(
        "/api/items", json={"raw_text": text},
        headers={"X-Life-Inbox-Timezone": zone},
    )
    assert response.status_code == 200, response.text
    return response.json()


def analyze(inbox):
    response = inbox.client.post("/api/analyze")
    assert response.status_code == 200, response.text
    return response.json()


def tasks(inbox):
    response = inbox.client.get("/api/tasks?status=all")
    assert response.status_code == 200, response.text
    return response.json()


def only_task(inbox):
    rows = tasks(inbox)
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.parametrize("first_text", ["Call dentist today", "Pay bill today"])
def test_multisource_timing_searches_all_sources_before_action_fallback(
    inbox, monkeypatch, first_text
):
    from app.organizer import fallback_analyze

    first = capture(inbox, first_text)
    second = capture(inbox, "Pay bill tomorrow")

    def merged_analysis(notes):
        result = fallback_analyze(notes)
        result["todos"] = [{
            "task": "Pay bill", "priority": "High", "when": "tomorrow",
            "source_note_ids": [first["id"], second["id"]],
        }]
        return result

    mock_analysis(monkeypatch, inbox.main, merged_analysis)
    analyze(inbox)
    task = only_task(inbox)
    assert task["due_on"] == "2026-10-16"
    assert task["ai_timing_text"] == "tomorrow"


def preview(inbox, task_id, **values):
    return inbox.client.post(
        "/api/calendar/preview", json={"task_id": task_id, **values}
    )


def test_calendar_draft_rejects_changed_deadline_timezone(inbox):
    capture(inbox)
    analyze(inbox)
    task = only_task(inbox)
    event = preview(inbox, task["id"])
    assert event.status_code == 200, event.text
    changed = inbox.client.patch(
        f"/api/tasks/{task['id']}/deadline", json={"due_on": task["due_on"]},
        headers={"X-Life-Inbox-Timezone": "America/New_York"},
    )
    assert changed.status_code == 200, changed.text
    assert inbox.client.post(
        "/api/calendar/confirm", json={"draft_id": event.json()["draft_id"]}
    ).status_code == 409
    assert preview(inbox, task["id"]).json()["timezone"] == "America/New_York"


def test_unrelated_source_rewrite_does_not_inherit_completed_task_or_its_correction(inbox):
    item = capture(inbox, "Pay tax today")
    analyze(inbox)
    old_task = only_task(inbox)
    inbox.client.patch(f"/api/tasks/{old_task['id']}/deadline", json={"due_on": "2026-10-25"})
    inbox.client.post(f"/api/tasks/{old_task['id']}/complete")
    at(inbox, "2026-10-20T10:00:00+00:00")
    inbox.client.put(
        f"/api/items/{item['id']}", json={"raw_text": "Call dentist tomorrow"},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    )
    analyze(inbox)
    rows = tasks(inbox)
    assert len(rows) == 2, rows
    retained = next(row for row in rows if row["id"] == old_task["id"])
    new_task = next(row for row in rows if row["id"] != old_task["id"])
    assert retained["status"] == "completed"
    assert retained["due_on"] == "2026-10-25"
    assert retained["source_changed"] is True
    assert new_task["status"] == "open"
    assert new_task["due_on"] == "2026-10-21"
    assert new_task["deadline_status"] == "inferred"


def test_delayed_first_analysis_uses_capture_timestamp_and_timezone(inbox):
    item = capture(inbox)
    assert item["time_reference_at"] == "2026-10-15T10:00:00+00:00"
    assert item["time_reference_timezone"] == "Asia/Kolkata"
    at(inbox, "2026-10-20T10:00:00+00:00")
    analyze(inbox)
    task = only_task(inbox)
    assert task["due_on"] == "2026-10-15"
    assert task["deadline_status"] == "inferred"
    assert task["deadline_reference_at"] == item["time_reference_at"]
    assert task["deadline_timezone"] == "Asia/Kolkata"
    assert "today" in task["ai_timing_text"].lower()
    board = inbox.client.get(
        "/api/attention", headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"}
    ).json()
    assert task["id"] in {row["id"] for row in board["overdue"]}
    event = preview(inbox, task["id"])
    assert event.status_code == 200, event.text
    assert event.json()["start_date"] == task["due_on"]
    assert event.json()["timezone"] == "Asia/Kolkata"


def test_reads_reanalysis_confirmation_and_restart_never_move_deadline(inbox):
    capture(inbox)
    analyze(inbox)
    original = only_task(inbox)
    at(inbox, "2026-10-20T10:00:00+00:00")
    for _ in range(2):
        assert only_task(inbox)["due_on"] == "2026-10-15"
        inbox.client.get("/api/attention")
        analyze(inbox)
    for payload in ({}, {"timing_text": original["timing_text"]}):
        response = inbox.client.post(
            f"/api/tasks/{original['id']}/confirm-timing", json=payload
        )
        assert response.status_code == 200, response.text
        assert response.json()["due_on"] == "2026-10-15"
        assert response.json()["deadline_status"] == "confirmed"
    analyze(inbox)
    assert only_task(inbox)["id"] == original["id"]
    inbox.db.engine.dispose()
    restarted = inbox.restart()
    saved = restarted.client.get("/api/tasks?status=all").json()
    assert len(saved) == 1
    assert saved[0]["id"] == original["id"]
    assert saved[0]["due_on"] == "2026-10-15"
    assert saved[0]["deadline_status"] == "confirmed"


def test_unchanged_source_reuses_task_despite_model_wording_and_date_drift(
    inbox, monkeypatch
):
    item = capture(inbox)
    response = {
        "buckets": [{"name": "Money & Admin", "notes": [{
            "id": item["id"], "title": "Electricity bill", "priority": "High",
        }]}],
        "todos": [{
            "task": "Pay electricity bill", "priority": "High",
            "source_note_ids": [item["id"]], "when": "today",
        }],
        "priority_view": {
            "High": [{"id": item["id"], "title": "Electricity bill"}],
            "Medium": [], "Low": [],
        },
        "mode": "openai",
    }
    mock_analysis(monkeypatch, inbox.main, lambda _notes: response)
    analyze(inbox)
    original = only_task(inbox)
    at(inbox, "2026-10-20T10:00:00+00:00")
    response["todos"][0].update(task="Settle the utility payment", when="tomorrow")
    analyze(inbox)
    saved = only_task(inbox)
    assert saved["id"] == original["id"]
    assert saved["due_on"] == "2026-10-15"
    assert saved["timing_text"] == original["timing_text"]


@pytest.mark.parametrize("corrected", ["2026-10-25", None])
def test_correction_or_clear_survives_reanalysis_and_source_edit(inbox, corrected):
    item = capture(inbox)
    analyze(inbox)
    task = only_task(inbox)
    at(inbox, "2026-10-20T10:00:00+00:00")
    response = inbox.client.patch(
        f"/api/tasks/{task['id']}/deadline", json={"due_on": corrected}
    )
    assert response.status_code == 200, response.text
    assert response.json()["deadline_status"] == ("edited" if corrected else "cleared")
    assert response.json()["due_on"] == corrected
    assert "today" in response.json()["ai_timing_text"].lower()
    analyze(inbox)
    assert only_task(inbox)["due_on"] == corrected
    updated = inbox.client.put(
        f"/api/items/{item['id']}",
        json={"raw_text": "Pay the electricity bill tomorrow"},
        headers={"X-Life-Inbox-Timezone": "Europe/London"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["time_reference_at"] == "2026-10-20T10:00:00+00:00"
    assert updated.json()["time_reference_timezone"] == "Europe/London"
    analyze(inbox)
    saved = only_task(inbox)
    assert saved["id"] == task["id"]
    assert saved["due_on"] == corrected
    assert saved["source_changed"] is True
    if corrected:
        event = preview(inbox, task["id"])
        assert event.status_code == 200, event.text
        assert event.json()["start_date"] == corrected
    else:
        assert preview(inbox, task["id"]).status_code == 400
        assert inbox.client.post(
            f"/api/tasks/{task['id']}/confirm-timing", json={}
        ).status_code == 400


def test_source_edit_references_edit_time_but_noop_edit_keeps_capture_time(inbox):
    item = capture(inbox)
    analyze(inbox)
    original = only_task(inbox)
    at(inbox, "2026-10-20T10:00:00+00:00")
    same = inbox.client.put(
        f"/api/items/{item['id']}", json={"raw_text": item["raw_text"]}
    ).json()
    assert same["time_reference_at"] == item["time_reference_at"]
    analyze(inbox)
    assert only_task(inbox)["due_on"] == "2026-10-15"
    inbox.client.put(
        f"/api/items/{item['id']}",
        json={"raw_text": "Pay the electricity bill tomorrow"},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    )
    analyze(inbox)
    saved = only_task(inbox)
    assert saved["id"] == original["id"]
    assert saved["due_on"] == "2026-10-21"
    assert saved["deadline_reference_at"] == "2026-10-20T10:00:00+00:00"
    assert saved["deadline_status"] == "inferred"
    assert saved["source_changed"] is False


@pytest.mark.parametrize(
    ("timestamp", "zone", "due"),
    [
        ("2026-10-14T22:30:00+00:00", "Asia/Kolkata", "2026-10-15"),
        ("2026-10-14T22:30:00+00:00", "UTC", "2026-10-14"),
        ("2026-03-08T06:30:00+00:00", "America/New_York", "2026-03-08"),
        ("2026-11-01T04:30:00+00:00", "America/New_York", "2026-11-01"),
        ("2026-11-01T06:30:00+00:00", "America/New_York", "2026-11-01"),
    ],
)
def test_date_only_deadline_uses_capture_local_day_at_midnight_and_dst(
    inbox, timestamp, zone, due
):
    at(inbox, timestamp)
    capture(inbox, zone=zone)
    at(inbox, "2026-11-20T10:00:00+00:00")
    analyze(inbox)
    task = only_task(inbox)
    assert task["due_on"] == due
    assert task["deadline_timezone"] == zone
    assert preview(inbox, task["id"]).json()["start_date"] == due


def test_attention_uses_viewer_local_day_without_changing_saved_date(inbox):
    at(inbox, "2026-10-14T22:30:00+00:00")
    capture(inbox)
    analyze(inbox)
    task = only_task(inbox)
    local = inbox.client.get(
        "/api/attention", headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"}
    ).json()
    utc = inbox.client.get(
        "/api/attention", headers={"X-Life-Inbox-Timezone": "UTC"}
    ).json()
    assert local["reference_date"] == "2026-10-15"
    assert task["id"] in {row["id"] for row in local["today"]}
    assert utc["reference_date"] == "2026-10-14"
    assert task["id"] in {row["id"] for row in utc["upcoming"]}
    assert only_task(inbox)["due_on"] == "2026-10-15"


@pytest.mark.parametrize("wording", ["2026-02-30", "tomorrow at 10am", "in 999999999 days"])
def test_invalid_or_unsupported_timing_needs_review_without_crashing(inbox, wording):
    capture(inbox, f"Pay the electricity bill {wording}")
    analyze(inbox)
    task = only_task(inbox)
    assert task["due_on"] is None
    assert task["deadline_needs_review"] is True
    assert task["deadline_note"]
    assert preview(inbox, task["id"]).status_code == 400
    assert inbox.client.post(
        f"/api/tasks/{task['id']}/confirm-timing", json={}
    ).status_code == 400
    board = inbox.client.get("/api/attention").json()
    assert task["id"] in {row["id"] for row in board["unscheduled"]}


def test_urgency_does_not_override_tomorrow(inbox):
    capture(inbox, "Pay the electricity bill urgent tomorrow")
    analyze(inbox)
    task = only_task(inbox)
    assert task["priority"] == "High"
    assert task["due_on"] == "2026-10-16"
    assert task["deadline_needs_review"] is False


def test_ambiguous_candidate_can_be_confirmed_without_reanchoring(inbox):
    capture(inbox, "Pay the electricity bill Friday")
    analyze(inbox)
    task = only_task(inbox)
    assert task["due_on"] == "2026-10-16"
    assert task["deadline_needs_review"] is True
    assert preview(inbox, task["id"]).status_code == 400
    at(inbox, "2026-10-20T10:00:00+00:00")
    confirmed = inbox.client.post(
        f"/api/tasks/{task['id']}/confirm-timing", json={}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["due_on"] == "2026-10-16"
    assert confirmed.json()["deadline_needs_review"] is False
    assert preview(inbox, task["id"]).json()["start_date"] == "2026-10-16"


def test_deadline_patch_validates_input_and_calendar_cannot_override_saved_date(inbox):
    capture(inbox)
    analyze(inbox)
    task = only_task(inbox)
    for payload in (
        {}, {"due_on": "2026-02-30"},
        {"due_on": "2026-10-15T10:00:00Z"},
        {"due_on": "2026-10-15T00:00:00Z"},
    ):
        response = inbox.client.patch(f"/api/tasks/{task['id']}/deadline", json=payload)
        assert response.status_code == 422, response.text
    assert only_task(inbox)["due_on"] == "2026-10-15"
    assert preview(inbox, task["id"], start_date="2026-10-21").status_code == 400


def test_invalid_timezone_is_rejected_and_request_timezone_does_not_leak(inbox):
    bad = inbox.client.post(
        "/api/items", json={"raw_text": "Pay bill today"},
        headers={"X-Life-Inbox-Timezone": "MadeUp/Nowhere"},
    )
    assert bad.status_code == 400, bad.text
    assert inbox.client.get("/api/items").json() == []
    first = capture(inbox)
    second = inbox.client.post("/api/items", json={"raw_text": "Call plumber today"}).json()
    assert first["time_reference_timezone"] == "Asia/Kolkata"
    assert second["time_reference_timezone"] == "UTC"


def test_retry_keeps_capture_reference_but_user_extraction_edit_sets_new_reference(
    inbox, monkeypatch
):
    real_extract = inbox.main.extract_pdf_text
    monkeypatch.setattr(
        inbox.main, "extract_pdf_text",
        lambda _data: (_ for _ in ()).throw(ValueError("Temporary extraction failure")),
    )
    captured = inbox.client.post(
        "/api/items/pdf",
        files={"file": ("bill.pdf", make_text_pdf("Pay electricity bill today"), "application/pdf")},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    ).json()
    assert captured["processing_status"] == "failed"
    at(inbox, "2026-10-20T10:00:00+00:00")
    monkeypatch.setattr(inbox.main, "extract_pdf_text", real_extract)
    processed = inbox.client.post(f"/api/items/{captured['id']}/retry").json()
    assert processed["processing_status"] == "ready", processed
    assert processed["time_reference_at"] == captured["time_reference_at"]
    analyze(inbox)
    assert only_task(inbox)["due_on"] == "2026-10-15"
    updated = inbox.client.patch(
        f"/api/items/{captured['id']}/extracted-text",
        json={"extracted_text": "Pay electricity bill tomorrow"},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    ).json()
    assert updated["time_reference_at"] == "2026-10-20T10:00:00+00:00"
    analyze(inbox)
    assert only_task(inbox)["due_on"] == "2026-10-21"


def test_new_deadline_actions_are_owner_scoped(inbox):
    assert inbox.client.post(
        "/api/auth/dev-login", json={"email": "deadline-owner@example.com"}
    ).status_code == 200
    capture(inbox, "Pay PRIVATE_ELECTRICITY bill today")
    analyze(inbox)
    task = only_task(inbox)
    with TestClient(inbox.main.app) as other:
        assert other.post(
            "/api/auth/dev-login", json={"email": "other-deadline-owner@example.com"}
        ).status_code == 200
        for payload in ({"due_on": "2026-11-01"}, {"due_on": None}):
            response = other.patch(f"/api/tasks/{task['id']}/deadline", json=payload)
            assert response.status_code == 404, response.text
            assert "PRIVATE_ELECTRICITY" not in response.text
        assert other.post(
            f"/api/tasks/{task['id']}/confirm-timing", json={}
        ).status_code == 404
        assert other.get("/api/tasks").json() == []
        assert "PRIVATE_ELECTRICITY" not in other.get("/api/attention").text
        assert other.post(
            "/api/calendar/preview", json={"task_id": task["id"]}
        ).status_code == 404
    assert only_task(inbox)["due_on"] == "2026-10-15"


@pytest.mark.parametrize(
    ("saved_date", "confirmed"),
    [("2026-10-15", False), ("2026-10-17", True), (None, False)],
)
def test_legacy_reads_and_analysis_never_invent_missing_historical_context(
    inbox, saved_date, confirmed
):
    item = capture(inbox)
    database = inbox.db
    with database.SessionLocal() as session:
        source = session.get(database.InboxItemRecord, item["id"])
        source.time_reference_at = None
        source.time_reference_timezone = None
        task = database.TaskRecord(
            text="Pay the electricity bill today", priority="High",
            timing_text="today", ai_timing_text="today",
            timing_confirmed=int(confirmed), due_on=saved_date,
            due_confidence="high" if saved_date else None,
            deadline_status="confirmed" if confirmed else "legacy",
            deadline_needs_review=0 if confirmed else 1,
            deadline_note="Saved before capture timezone tracking.",
            status="open", dedup_key="legacy-electricity-task",
            owner_user_id=item["owner_user_id"],
            created_at="2026-10-15T10:00:00+00:00",
            updated_at="2026-10-15T10:00:00+00:00",
        )
        session.add(task)
        session.flush()
        task_id = task.id
        session.add(database.TaskSourceItemRecord(task_id=task_id, item_id=item["id"]))
        session.commit()
    at(inbox, "2026-10-20T10:00:00+00:00")
    for _ in range(2):
        row = only_task(inbox)
        assert row["due_on"] == saved_date
        assert row["deadline_reference_at"] is None
        assert row["deadline_timezone"] is None
        assert row["deadline_needs_review"] is (not confirmed)
        board = inbox.client.get("/api/attention").json()
        bucket = "overdue" if saved_date else "unscheduled"
        assert task_id in {entry["id"] for entry in board[bucket]}
        analyze(inbox)
    with database.SessionLocal() as session:
        saved = session.get(database.TaskRecord, task_id)
        assert saved.due_on == saved_date
        assert saved.deadline_reference_at is None
        assert saved.deadline_timezone is None


@pytest.mark.parametrize("corrected", ["2026-10-25", None])
def test_calendar_draft_cannot_be_confirmed_after_deadline_change(inbox, corrected):
    capture(inbox)
    analyze(inbox)
    task = only_task(inbox)
    event = preview(inbox, task["id"])
    assert event.status_code == 200, event.text
    updated = inbox.client.patch(
        f"/api/tasks/{task['id']}/deadline", json={"due_on": corrected}
    )
    assert updated.status_code == 200, updated.text
    for _ in range(2):
        response = inbox.client.post(
            "/api/calendar/confirm", json={"draft_id": event.json()["draft_id"]}
        )
        assert response.status_code == 409, response.text
    with inbox.db.SessionLocal() as session:
        assert session.scalars(
            inbox.db.select(inbox.db.CalendarActionReceiptRecord)
        ).all() == []


def test_multiple_actions_keep_distinct_dates_and_no_duplicate_tasks(inbox):
    item = capture(inbox, "Pay electricity bill today. Call the dentist tomorrow.")
    analyze(inbox)
    original = tasks(inbox)
    assert len(original) == 2, original
    by_text = {task["text"]: task for task in original}
    bill = next(task for text, task in by_text.items() if "bill" in text)
    dentist = next(task for text, task in by_text.items() if "dentist" in text)
    assert bill["due_on"] == "2026-10-15"
    assert dentist["due_on"] == "2026-10-16"
    assert bill["source_item_ids"] == dentist["source_item_ids"] == [item["id"]]
    at(inbox, "2026-10-20T10:00:00+00:00")
    analyze(inbox)
    again = tasks(inbox)
    assert {(row["id"], row["due_on"]) for row in again} == {
        (row["id"], row["due_on"]) for row in original
    }


def test_unchanged_edited_source_does_not_duplicate_when_old_actions_are_retained(
    inbox, monkeypatch
):
    item = capture(inbox, "Pay electricity bill today. Call the dentist tomorrow.")
    analyze(inbox)
    at(inbox, "2026-10-20T10:00:00+00:00")
    inbox.client.put(
        f"/api/items/{item['id']}",
        json={"raw_text": "Pay electricity bill tomorrow."},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    )
    analyze(inbox)
    after_edit = tasks(inbox)
    # Retaining a stale old task is a product choice; processing the same new
    # content again must still preserve the current durable task identities.
    response = {
        "buckets": [{"name": "Money & Admin", "notes": [{
            "id": item["id"], "title": "Utility payment", "priority": "High",
        }]}],
        "todos": [{
            "task": "Settle outstanding utility account", "priority": "High",
            "source_note_ids": [item["id"]], "when": "tomorrow",
        }],
        "priority_view": {
            "High": [{"id": item["id"], "title": "Utility payment"}],
            "Medium": [], "Low": [],
        },
        "mode": "openai",
    }
    mock_analysis(monkeypatch, inbox.main, lambda _notes: response)
    analyze(inbox)
    assert {(row["id"], row["due_on"]) for row in tasks(inbox)} == {
        (row["id"], row["due_on"]) for row in after_edit
    }


def test_source_changed_during_analysis_rejects_stale_results(inbox, monkeypatch):
    item = capture(inbox)
    at(inbox, "2026-10-20T10:00:00+00:00")
    original_analyzer = inbox.main.propose_delta

    def analyze_while_editing(batch):
        assert batch["targets"][0]["text"] == item["raw_text"]
        inbox.db.update_item_in_db(item["id"], "Pay the electricity bill tomorrow")
        return original_analyzer(batch)

    monkeypatch.setattr(inbox.main, "propose_delta", analyze_while_editing)
    response = inbox.client.post("/api/analyze")
    assert response.status_code == 409, response.text
    assert tasks(inbox) == []
    assert inbox.client.get("/api/analyze/latest").status_code == 204
    with inbox.db.SessionLocal() as session:
        source = session.get(inbox.db.InboxItemRecord, item["id"])
        assert source.task_analysis_fingerprint is None
        assert source.raw_text == "Pay the electricity bill tomorrow"
    monkeypatch.setattr(inbox.main, "propose_delta", original_analyzer)
    analyze(inbox)
    assert only_task(inbox)["due_on"] == "2026-10-21"


@pytest.mark.parametrize("second_zone", ["UTC", "Asia/Kolkata"])
def test_multi_source_relative_deadline_requires_review_when_contexts_differ(
    inbox, monkeypatch, second_zone
):
    first = capture(inbox, "Pay electricity bill today")
    if second_zone == "Asia/Kolkata":
        at(inbox, "2026-10-20T10:00:00+00:00")
    second = capture(inbox, "Pay utility account today", zone=second_zone)
    response = {
        "buckets": [{"name": "Money & Admin", "notes": [
            {"id": item["id"], "title": "Utility payment", "priority": "High"}
            for item in (first, second)
        ]}],
        "todos": [{
            "task": "Pay utility account", "priority": "High",
            "source_note_ids": [first["id"], second["id"]], "when": "today",
        }],
        "priority_view": {
            "High": [{"id": item["id"], "title": "Utility payment"}
                     for item in (first, second)],
            "Medium": [], "Low": [],
        },
        "mode": "openai",
    }
    mock_analysis(monkeypatch, inbox.main, lambda _notes: response)
    analyze(inbox)
    task = only_task(inbox)
    assert task["due_on"] is None
    assert task["deadline_needs_review"] is True
    assert preview(inbox, task["id"]).status_code == 400
    corrected = inbox.client.patch(
        f"/api/tasks/{task['id']}/deadline", json={"due_on": "2026-10-25"}
    )
    assert corrected.status_code == 200, corrected.text
    assert preview(inbox, task["id"]).json()["start_date"] == "2026-10-25"


def test_source_edit_blocks_stale_inferred_calendar_date_until_reanalysis(inbox):
    item = capture(inbox)
    analyze(inbox)
    task = only_task(inbox)
    event = preview(inbox, task["id"])
    assert event.status_code == 200, event.text
    at(inbox, "2026-10-20T10:00:00+00:00")
    inbox.client.put(
        f"/api/items/{item['id']}", json={"raw_text": "Pay electricity bill tomorrow"}
    )
    saved = only_task(inbox)
    assert saved["due_on"] == "2026-10-15"
    assert saved["source_changed"] is True
    assert preview(inbox, task["id"]).status_code == 400
    assert inbox.client.post(
        "/api/calendar/confirm", json={"draft_id": event.json()["draft_id"]}
    ).status_code == 409
    analyze(inbox)
    assert only_task(inbox)["due_on"] == "2026-10-21"
    assert preview(inbox, task["id"]).json()["start_date"] == "2026-10-21"


@pytest.mark.parametrize("corrected", [False, True])
def test_short_action_source_edit_retains_task_when_another_action_is_added(
    inbox, corrected
):
    item = capture(inbox, "Pay tax today")
    analyze(inbox)
    tax_task = only_task(inbox)
    if corrected:
        edited = inbox.client.patch(
            f"/api/tasks/{tax_task['id']}/deadline", json={"due_on": "2026-10-25"}
        )
        assert edited.status_code == 200, edited.text
    at(inbox, "2026-10-20T10:00:00+00:00")
    response = inbox.client.put(
        f"/api/items/{item['id']}",
        json={"raw_text": "Pay tax tomorrow. Call dentist tomorrow"},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    )
    assert response.status_code == 200, response.text
    analyze(inbox)
    rows = tasks(inbox)
    assert len(rows) == 2, rows
    saved_tax = next(row for row in rows if "tax" in row["text"].lower())
    dentist = next(row for row in rows if "dentist" in row["text"].lower())
    assert saved_tax["id"] == tax_task["id"]
    assert saved_tax["due_on"] == ("2026-10-25" if corrected else "2026-10-21")
    assert dentist["id"] != saved_tax["id"]
    assert dentist["due_on"] == "2026-10-21"
    analyze(inbox)
    assert {row["id"] for row in tasks(inbox)} == {saved_tax["id"], dentist["id"]}


@pytest.mark.parametrize("model_when", [None, "tomorrow"])
def test_model_cannot_discard_unsupported_time_from_source(inbox, monkeypatch, model_when):
    item = capture(inbox, "Pay bill tomorrow at 10a.m.")
    response = {
        "buckets": [{"name": "Money & Admin", "notes": [{
            "id": item["id"], "title": "Bill payment", "priority": "High",
        }]}],
        "todos": [{
            "task": "Pay bill", "priority": "High",
            "source_note_ids": [item["id"]], "when": model_when,
        }],
        "priority_view": {
            "High": [{"id": item["id"], "title": "Bill payment"}],
            "Medium": [], "Low": [],
        },
        "mode": "openai",
    }
    mock_analysis(monkeypatch, inbox.main, lambda _notes: response)
    analyze(inbox)
    task = only_task(inbox)
    assert task["due_on"] is None
    assert task["deadline_needs_review"] is True
    assert "10a.m" in task["ai_timing_text"].lower()
    assert "tomorrow" in task["ai_timing_text"].lower()
    assert preview(inbox, task["id"]).status_code == 400


def test_model_source_group_drift_cannot_duplicate_unchanged_analyzed_sources(
    inbox, monkeypatch
):
    first = capture(inbox, "Pay electricity bill today")
    second = capture(inbox, "Call dentist tomorrow")
    analyze(inbox)
    original = tasks(inbox)
    assert len(original) == 2
    at(inbox, "2026-10-20T10:00:00+00:00")
    response = {
        "buckets": [{"name": "Personal / Reference", "notes": [
            {"id": item["id"], "title": "Daily obligations", "priority": "High"}
            for item in (first, second)
        ]}],
        "todos": [{
            "task": "Handle the bill and dentist obligations", "priority": "High",
            "source_note_ids": [first["id"], second["id"]], "when": "tomorrow",
        }],
        "priority_view": {
            "High": [{"id": item["id"], "title": "Daily obligations"}
                     for item in (first, second)],
            "Medium": [], "Low": [],
        },
        "mode": "openai",
    }
    mock_analysis(monkeypatch, inbox.main, lambda _notes: response)
    analyze(inbox)
    assert {(row["id"], row["due_on"]) for row in tasks(inbox)} == {
        (row["id"], row["due_on"]) for row in original
    }
