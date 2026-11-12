"""Explicit task presentation choices survive inference and owner changes."""
import copy
import sys

from fastapi.testclient import TestClient
import pytest

from tests.test_api import client


def create_task(client, text="Call the team tomorrow"):
    item = client.post("/api/items", json={"raw_text": text}).json()
    response = client.post("/api/analyze")
    assert response.status_code == 200, response.text
    task = next(task for task in client.get("/api/tasks?status=all").json()
                if item["id"] in task["source_item_ids"])
    return item, task


def test_task_correction_preserves_inference_deadline_completion_and_source(client):
    item, original = create_task(client)
    client.patch(f"/api/tasks/{original['id']}/deadline", json={"due_on": "2026-12-10"})
    before = client.post(f"/api/tasks/{original['id']}/complete").json()
    response = client.patch(f"/api/tasks/{original['id']}", json={"text": "Team catch-up", "priority": "Low"})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["text"] == saved["user_text"] == "Team catch-up"
    assert saved["priority"] == saved["user_priority"] == "Low"
    assert saved["ai_text"] == before["ai_text"] == original["text"]
    assert saved["ai_priority"] == before["ai_priority"] == original["priority"]
    assert saved["has_user_overrides"] is True
    for field in ("id", "status", "due_on", "due_confidence", "deadline_status", "deadline_reference_at",
                  "deadline_timezone", "deadline_note", "deadline_needs_review", "timing_text", "ai_timing_text",
                  "source_item_ids", "source_changed", "created_at", "snoozed_until", "reminder_at"):
        assert saved[field] == before[field], field
    db = sys.modules["app.database"]
    with db.SessionLocal() as session:
        row = session.get(db.TaskRecord, saved["id"])
        assert row.text == original["text"]
        assert row.priority == original["priority"]
    assert client.put(f"/api/items/{item['id']}", json={"raw_text": "Call the team tomorrow about lunch"}).status_code == 200
    assert client.post("/api/analyze").status_code == 200
    db.engine.dispose()
    with TestClient(client.app) as reopened:
        tasks = reopened.get("/api/tasks?status=all").json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == original["id"]
    assert tasks[0]["text"] == "Team catch-up"
    assert tasks[0]["priority"] == "Low"
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["due_on"] == "2026-12-10"
    assert tasks[0]["deadline_status"] == "edited"
    reset = client.patch(f"/api/tasks/{original['id']}", json={"clear_overrides": True}).json()
    assert reset["text"] == reset["ai_text"]
    assert reset["priority"] == reset["ai_priority"]
    assert reset["has_user_overrides"] is False
    assert reset["status"] == "completed"
    assert reset["due_on"] == "2026-12-10"


def test_task_corrections_do_not_invalidate_source_or_call_organizer(client, monkeypatch):
    item, task = create_task(client)
    before = client.get("/api/analysis/status").json()
    source = client.get(f"/api/items/{item['id']}").json()
    main = sys.modules["app.main"]
    monkeypatch.setattr(main, "propose_delta", lambda batch: (_ for _ in ()).throw(AssertionError("unchanged input called organizer")))
    response = client.patch(f"/api/tasks/{task['id']}", json={"text": "Call home", "priority": "Low"})
    assert response.status_code == 200
    assert client.get("/api/analysis/status").json() == before
    assert client.get(f"/api/items/{item['id']}").json() == source
    assert client.post("/api/analyze").json()["metrics"]["target_count"] == 0
    detail = client.get(f"/api/items/{item['id']}/detail").json()
    assert detail["tasks"][0]["text"] == "Call home"
    board = client.get("/api/attention").json()
    entries = board["today"] + board["upcoming"] + board["overdue"] + board["unscheduled"]
    assert next(row for row in entries if row["id"] == task["id"])["priority"] == "Low"
    assert client.get("/api/analyze/latest").json()["todos"][0]["task"] == "Call home"


@pytest.mark.parametrize("payload", [
    {}, {"text": None}, {"text": ""}, {"text": " \n "}, {"text": "x" * 2001},
    {"priority": "Urgent"}, {"priority": 1}, {"clear_overrides": "true"},
    {"text": "New", "clear_overrides": True}, {"priority": "Low", "clear_overrides": True},
    {"text": "New", "status": "completed"}, {"text": "New", "due_on": "2026-12-10"},
])
def test_task_patch_rejects_unsupported_or_ambiguous_updates(client, payload):
    _, task = create_task(client)
    response = client.patch(f"/api/tasks/{task['id']}", json=payload)
    assert response.status_code == 422, response.text
    assert client.get("/api/tasks?status=all").json()[0] == task


def test_task_corrections_are_owner_scoped(client):
    client.post("/api/auth/dev-login", json={"email": "task-correction-a@example.com"})
    _, task = create_task(client)
    before = client.get("/api/tasks?status=all").json()
    with TestClient(client.app) as other:
        other.post("/api/auth/dev-login", json={"email": "task-correction-b@example.com"})
        _, own_task = create_task(other, "Call private colleague today")
        assert other.patch(f"/api/tasks/{task['id']}", json={"text": "Overwritten"}).status_code == 404
        assert other.patch(f"/api/tasks/{task['id']}", json={"clear_overrides": True}).status_code == 404
        assert client.patch(f"/api/tasks/{own_task['id']}", json={"priority": "Low"}).status_code == 404
        assert other.patch("/api/tasks/999999", json={"text": "Missing"}).status_code == 404
    assert client.get("/api/tasks?status=all").json() == before


def test_effective_task_meaning_controls_cross_capture_matching_and_context(client, monkeypatch):
    _, task = create_task(client, "Call dentist today")
    corrected = client.patch(f"/api/tasks/{task['id']}", json={"text": "Call doctor today", "priority": "Low"}).json()
    incoming = client.post("/api/items", json={"raw_text": "Call dentist today"}).json()
    main = sys.modules["app.main"]
    planner = main.propose_delta
    batches = []

    def observe(batch):
        batches.append(copy.deepcopy(batch))
        return planner(batch)

    monkeypatch.setattr(main, "propose_delta", observe)
    response = client.post("/api/analyze")
    assert response.status_code == 200, response.text
    assert len(batches) == 1
    supporting = next(row for row in batches[0]["supporting_context"]["tasks"] if row["id"] == task["id"])
    assert supporting["text"] == supporting["user_text"] == "Call doctor today"
    assert supporting["priority"] == supporting["user_priority"] == "Low"
    assert supporting["ai_text"] == "Call dentist today"
    assert supporting["has_user_overrides"] is True
    saved = client.get("/api/tasks?status=all").json()
    assert len(saved) == 2
    retained = next(row for row in saved if row["id"] == task["id"])
    assert retained == corrected
    assert incoming["id"] not in retained["source_item_ids"]


def test_task_correction_during_planning_rejects_stale_context(client, monkeypatch):
    _, task = create_task(client, "Pay invoice INV-123 today")
    incoming = client.post("/api/items", json={"raw_text": "Invoice INV-123 includes shipping"}).json()
    main = sys.modules["app.main"]
    planner = main.propose_delta

    def edit_while_planning(batch):
        response = client.patch(f"/api/tasks/{task['id']}", json={"text": "My corrected bill", "priority": "Low"})
        assert response.status_code == 200
        return planner(batch)

    monkeypatch.setattr(main, "propose_delta", edit_while_planning)
    response = client.post("/api/analyze")
    assert response.status_code == 409, response.text
    saved = client.get("/api/tasks?status=all").json()
    assert len(saved) == 1
    assert saved[0]["text"] == "My corrected bill"
    assert saved[0]["priority"] == "Low"
    assert incoming["id"] not in saved[0]["source_item_ids"]


def test_review_and_processing_labels_use_owned_effective_values(client):
    client.post("/api/auth/dev-login", json={"email": "review-label-a@example.com"})
    item, task = create_task(client)
    client.patch(f"/api/items/{item['id']}/insight", json={"user_title": "My capture label"})
    client.patch(f"/api/tasks/{task['id']}", json={"text": "My task label"})
    db = sys.modules["app.database"]
    with TestClient(client.app) as other:
        other.post("/api/auth/dev-login", json={"email": "review-label-b@example.com"})
        foreign, foreign_task = create_task(other, "Call PRIVATE colleague today")
        with db.SessionLocal() as session:
            state = session.get(db.ItemAnalysisStateRecord, item["id"])
            for key, candidate in (("owned-label", task["id"]), ("invalid-foreign-label", foreign_task["id"])):
                session.add(db.AnalysisReviewRecord(owner_user_id=item["owner_user_id"], run_id=state.last_run_id,
                    item_id=item["id"], candidate_task_id=candidate, kind="possible_duplicate", reason="Needs review",
                    status="pending", dedup_key=key, created_at=item["created_at"]))
            session.commit()
        states = client.get("/api/analysis/status").json()
        assert len(states) == 1
        assert states[0]["title"] == "My capture label"
        reviews = client.get("/api/analysis/reviews").json()
        assert len(reviews) == 2
        own = next(row for row in reviews if row["candidate_task_id"] == task["id"])
        assert own["source_title"] == "My capture label"
        assert own["candidate_task_text"] == "My task label"
        assert next(row for row in reviews if row["candidate_task_id"] is None)["candidate_task_text"] is None
        assert "PRIVATE" not in str(reviews)
        assert {row["item_id"] for row in other.get("/api/analysis/status").json()} == {foreign["id"]}
        assert other.get("/api/analysis/reviews").json() == []


def test_simultaneous_processing_reads_initialize_each_source_once(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from sqlalchemy import event, select

    item = client.post('/api/items', json={'raw_text': 'Call the workshop tomorrow'}).json()
    db = sys.modules['app.database']
    barrier, seen = Barrier(2), local()

    def both_read_missing_state(connection, cursor, statement, parameters, context, executemany):
        if (statement.startswith('SELECT item_analysis_state.')
                and 'WHERE item_analysis_state.item_id = ' in statement
                and not getattr(seen, 'waited', False)):
            seen.waited = True
            barrier.wait(timeout=10)

    event.listen(db.engine, 'after_cursor_execute', both_read_missing_state)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.get('/api/analysis/status'), range(2)))
    finally:
        event.remove(db.engine, 'after_cursor_execute', both_read_missing_state)
    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json()[0]['item_id'] == item['id'] for response in responses)
    with db.SessionLocal() as session:
        assert len(session.scalars(select(db.ItemAnalysisStateRecord)).all()) == 1
