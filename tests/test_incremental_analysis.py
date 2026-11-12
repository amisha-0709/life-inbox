"""The organizer consumes a bounded delta and preserves unrelated durable facts."""

from contextlib import ExitStack
from datetime import datetime, timezone
import copy
import importlib
import json
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text


@pytest.fixture()
def incremental(tmp_path, monkeypatch):
    database_path = tmp_path / "incremental.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("LIFE_INBOX_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("LIFE_INBOX_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LIFE_INBOX_ENV", "local")
    clock = SimpleNamespace(instant=datetime(2026, 10, 15, 10, tzinfo=timezone.utc))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.instant.astimezone(tz) if tz else clock.instant.replace(tzinfo=None)

    def purge():
        for name in tuple(sys.modules):
            if name in {
                "app.main", "app.database", "app.auth", "app.config",
                "app.integrations.calendar", "app.retrieval.ask",
            } or name.startswith("app.analysis"):
                sys.modules.pop(name, None)

    with ExitStack() as stack:
        engines = []

        def start():
            purge()
            main = importlib.import_module("app.main")
            database = importlib.import_module("app.database")
            monkeypatch.setattr(database, "datetime", Clock)
            monkeypatch.setattr(importlib.import_module("app.attention.dates"), "datetime", Clock)
            assert database.engine.url.database == str(database_path)
            engines.append(database.engine)
            client = stack.enter_context(TestClient(main.app))
            return SimpleNamespace(main=main, db=database, client=client)

        env = start()
        env.restart = start
        env.clock = clock
        env.new_client = lambda: stack.enter_context(TestClient(env.main.app))
        yield env
    for engine in engines:
        engine.dispose()
    purge()


def capture(env, source="Pay electricity bill today"):
    response = env.client.post(
        "/api/items", json={"raw_text": source},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def analyze(env):
    response = env.client.post("/api/analyze")
    assert response.status_code == 200, response.text
    return response.json()


def tasks(env):
    response = env.client.get("/api/tasks?status=all")
    assert response.status_code == 200, response.text
    return response.json()


def states(env):
    response = env.client.get("/api/analysis/status")
    assert response.status_code == 200, response.text
    return {row["item_id"]: row for row in response.json()}


def invalidate(env, item_id):
    response = env.client.post(f"/api/items/{item_id}/invalidate-analysis")
    assert response.status_code == 200, response.text
    return response.json()


def forbid_planner(monkeypatch, env):
    def unexpected_call(_batch):
        pytest.fail("An unchanged inbox must not invoke the organizer")

    monkeypatch.setattr(env.main, "propose_delta", unexpected_call)


def test_empty_and_unchanged_inboxes_make_zero_organizer_calls(incremental, monkeypatch):
    planner = incremental.main.propose_delta
    forbid_planner(monkeypatch, incremental)
    empty = analyze(incremental)
    assert empty["metrics"]["noop"] is True
    assert empty["metrics"]["target_count"] == 0
    assert empty["metrics"]["model_calls"] == 0
    monkeypatch.setattr(incremental.main, "propose_delta", planner)
    capture(incremental)
    analyze(incremental)
    before = tasks(incremental)
    forbid_planner(monkeypatch, incremental)
    again = analyze(incremental)
    assert again["metrics"]["noop"] is True
    assert again["metrics"]["target_ids"] == []
    assert again["metrics"]["model_calls"] == 0
    assert tasks(incremental) == before


def test_one_new_note_is_the_only_target_and_old_rows_do_not_change(incremental, monkeypatch):
    old_items = [capture(incremental, source) for source in (
        "Pay electricity bill today", "Call violin teacher tomorrow",
        "Buy gardening gloves", "Renew passport on 2026-12-10",
        "Send quarterly report", "Book dental cleaning on 2026-11-15",
    )]
    first = analyze(incremental)
    assert first["metrics"]["target_count"] == len(old_items)
    before = copy.deepcopy(tasks(incremental))
    insights = {
        item["id"]: incremental.client.get(f"/api/items/{item['id']}/insight").json()
        for item in old_items
    }
    new = capture(incremental, "Call violin teacher tomorrow about lesson location")
    batches = []
    planner = incremental.main.propose_delta

    def observe(batch):
        batches.append(batch)
        return planner(batch)

    monkeypatch.setattr(incremental.main, "propose_delta", observe)
    result = analyze(incremental)
    metrics = result["metrics"]
    assert metrics["target_count"] == 1
    assert metrics["target_ids"] == [new["id"]]
    assert 0 < metrics["context_task_count"] <= 8
    assert metrics["context_snippet_count"] <= 8
    assert metrics["context_characters"] <= 10000
    assert metrics["model_calls"] == 0
    # No provider call consumed tokens; this zero is exact, not an estimate.
    assert metrics["input_tokens"] == 0
    assert metrics["estimated_input_tokens"] >= 0
    assert len(batches) == 1
    assert [target["id"] for target in batches[0]["targets"]] == [new["id"]]
    assert all(task["id"] in {row["id"] for row in before} for task in batches[0]["supporting_context"]["tasks"])
    assert new["id"] not in {snippet["id"] for snippet in batches[0]["supporting_context"]["snippets"]}
    by_id = {row["id"]: row for row in tasks(incremental)}
    # Enrichment can attach provenance to the matched task, but unrelated tasks
    # and all existing organization fields must remain byte-for-byte stable.
    for row in before:
        if "violin" not in row["text"]:
            assert by_id[row["id"]] == row
    for item_id, saved in insights.items():
        assert incremental.client.get(f"/api/items/{item_id}/insight").json() == saved


def test_reads_presentation_edits_and_task_choices_do_not_invalidate(incremental, monkeypatch):
    item = capture(incremental)
    analyze(incremental)
    task = tasks(incremental)[0]
    incremental.client.patch(f"/api/items/{item['id']}/insight", json={"user_title": "My power bill"})
    incremental.client.patch(f"/api/tasks/{task['id']}/deadline", json={"due_on": "2026-10-25"})
    incremental.client.post(f"/api/tasks/{task['id']}/complete")
    saved = tasks(incremental)
    for path in ("/api/items", "/api/dashboard", "/api/attention", "/api/analyze/latest", "/api/tasks?status=all"):
        assert incremental.client.get(path).status_code == 200
    forbid_planner(monkeypatch, incremental)
    assert analyze(incremental)["metrics"]["target_count"] == 0
    assert tasks(incremental) == saved
    state = states(incremental)[item["id"]]
    assert state["status"] == "processed"
    assert state["input_fingerprint"]
    assert state["analysis_version"]


def test_processing_state_survives_restart(incremental, monkeypatch):
    item = capture(incremental)
    analyze(incremental)
    before_state = states(incremental)[item["id"]]
    before_tasks = tasks(incremental)
    restarted = incremental.restart()
    forbid_planner(monkeypatch, restarted)
    assert analyze(restarted)["metrics"]["target_count"] == 0
    assert states(restarted)[item["id"]]["input_fingerprint"] == before_state["input_fingerprint"]
    assert tasks(restarted) == before_tasks


def test_content_change_and_explicit_invalidation_are_deliberate_targets(incremental):
    first = capture(incremental)
    other = capture(incremental, "Buy gardening gloves")
    analyze(incremental)
    old = states(incremental)[first["id"]]
    assert incremental.client.patch(f"/api/items/{first['id']}/insight", json={
        "needs_review": True,
    }).status_code == 200
    response = incremental.client.put(
        f"/api/items/{first['id']}", json={"raw_text": "Pay electricity bill today, amount 2100"},
    )
    assert response.status_code == 200, response.text
    assert analyze(incremental)["metrics"]["target_ids"] == [first["id"]]
    assert states(incremental)[first["id"]]["input_fingerprint"] != old["input_fingerprint"]
    assert incremental.client.get(f"/api/items/{first['id']}/insight").json()["needs_review"] is True
    saved = states(incremental)[other["id"]]
    invalidate(incremental, other["id"])
    assert analyze(incremental)["metrics"]["target_ids"] == [other["id"]]
    updated = states(incremental)[other["id"]]
    assert updated["processed_generation"] == saved["processed_generation"] + 1
    assert updated["processed_generation"] == updated["invalidation_generation"]


def test_target_batches_are_bounded_and_pending_items_are_not_forgotten(incremental):
    items = [capture(incremental, f"Research project number {index}") for index in range(9)]
    first = analyze(incremental)["metrics"]
    assert first["target_count"] == 8
    assert first["pending_count"] == 1
    second = analyze(incremental)["metrics"]
    assert second["target_count"] == 1
    assert second["pending_count"] == 0
    assert set(first["target_ids"]) | set(second["target_ids"]) == {item["id"] for item in items}
    assert not set(first["target_ids"]) & set(second["target_ids"])


def test_oversized_source_is_blocked_without_silently_truncating_it(incremental, monkeypatch):
    item = capture(incremental)
    source = "Pay electricity bill today " + "original source " * 1800
    with incremental.db.engine.begin() as connection:
        connection.execute(
            text("UPDATE inbox_items SET raw_text = :source WHERE id = :item_id"),
            {"source": source, "item_id": item["id"]},
        )
    forbid_planner(monkeypatch, incremental)
    assert analyze(incremental)["metrics"]["target_count"] == 0
    state = states(incremental)[item["id"]]
    assert state["status"] == "blocked"
    assert state["error"]
    assert incremental.client.get(f"/api/items/{item['id']}").json()["raw_text"] == source


def test_source_change_while_planning_rejects_all_results(incremental, monkeypatch):
    item = capture(incremental)
    planner = incremental.main.propose_delta

    def edit_during_analysis(batch):
        response = incremental.client.put(
            f"/api/items/{item['id']}", json={"raw_text": "Pay water bill tomorrow"},
        )
        assert response.status_code == 200, response.text
        return planner(batch)

    monkeypatch.setattr(incremental.main, "propose_delta", edit_during_analysis)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 409, response.text
    assert tasks(incremental) == []
    assert incremental.client.get(f"/api/items/{item['id']}/insight").json()["analysis_pending"] is True
    with incremental.db.SessionLocal() as session:
        assert session.get(incremental.db.ItemInsightRecord, item["id"]) is None
    monkeypatch.setattr(incremental.main, "propose_delta", planner)
    assert analyze(incremental)["metrics"]["target_ids"] == [item["id"]]


def test_invalidation_during_analysis_is_not_lost(incremental, monkeypatch):
    item = capture(incremental)
    planner = incremental.main.propose_delta

    def invalidate_during_analysis(batch):
        invalidate(incremental, item["id"])
        return planner(batch)

    monkeypatch.setattr(incremental.main, "propose_delta", invalidate_during_analysis)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 409, response.text
    assert tasks(incremental) == []
    monkeypatch.setattr(incremental.main, "propose_delta", planner)
    assert analyze(incremental)["metrics"]["target_ids"] == [item["id"]]
    state = states(incremental)[item["id"]]
    assert state["processed_generation"] == state["invalidation_generation"]


def test_concurrent_user_correction_rejects_stale_supporting_context(incremental, monkeypatch):
    capture(incremental)
    analyze(incremental)
    original = tasks(incremental)[0]
    capture(incremental)
    planner = incremental.main.propose_delta

    def correct_during_analysis(batch):
        result = planner(batch)
        response = incremental.client.patch(
            f"/api/tasks/{original['id']}/deadline", json={"due_on": "2026-10-25"},
        )
        assert response.status_code == 200, response.text
        return result

    monkeypatch.setattr(incremental.main, "propose_delta", correct_during_analysis)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 409, response.text
    rows = tasks(incremental)
    assert len(rows) == 1
    assert rows[0]["id"] == original["id"]
    assert rows[0]["due_on"] == "2026-10-25"
    assert rows[0]["deadline_status"] == "edited"


def test_failure_is_persistent_and_explicit_retry_preserves_previous_results(incremental, monkeypatch):
    capture(incremental, "Buy gardening gloves")
    analyze(incremental)
    before = tasks(incremental)
    new = capture(incremental)
    planner = incremental.main.propose_delta

    def fail(_batch):
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(incremental.main, "propose_delta", fail)
    assert incremental.client.post("/api/analyze").status_code == 500
    assert states(incremental)[new["id"]]["status"] == "failed"
    assert tasks(incremental) == before
    forbid_planner(monkeypatch, incremental)
    assert analyze(incremental)["metrics"]["target_count"] == 0
    invalidate(incremental, new["id"])
    monkeypatch.setattr(incremental.main, "propose_delta", planner)
    assert analyze(incremental)["metrics"]["target_ids"] == [new["id"]]


def test_matching_legacy_processed_records_are_bootstrapped_without_reanalysis(incremental, monkeypatch):
    item = capture(incremental)
    analyze(incremental)
    before = tasks(incremental)
    with incremental.db.engine.begin() as connection:
        connection.execute(text("DELETE FROM item_analysis_state WHERE item_id = :item_id"), {"item_id": item["id"]})
    forbid_planner(monkeypatch, incremental)
    assert analyze(incremental)["metrics"]["target_count"] == 0
    assert tasks(incremental) == before
    assert states(incremental)[item["id"]]["input_fingerprint"]


def test_duplicate_captures_within_and_after_a_batch_attach_provenance(incremental):
    first = capture(incremental)
    second = capture(incremental)
    analyze(incremental)
    saved = tasks(incremental)
    assert len(saved) == 1, saved
    assert saved[0]["source_item_ids"] == [first["id"], second["id"]]
    third = capture(incremental)
    result = analyze(incremental)
    assert result["metrics"]["target_ids"] == [third["id"]]
    after = tasks(incremental)
    assert len(after) == 1
    assert after[0]["id"] == saved[0]["id"]
    assert after[0]["due_on"] == saved[0]["due_on"]
    assert after[0]["deadline_reference_at"] == saved[0]["deadline_reference_at"]
    assert after[0]["source_item_ids"] == [first["id"], second["id"], third["id"]]
    assert after[0]["source_changed"] is False


def test_additional_details_enrich_existing_task_without_replacing_it(incremental):
    first = capture(incremental, "Pay invoice ELEC-123 today")
    analyze(incremental)
    before = tasks(incremental)[0]
    second = capture(incremental, "Pay invoice ELEC-123 today using account 12345")
    analyze(incremental)
    after = tasks(incremental)
    assert len(after) == 1, after
    assert after[0]["id"] == before["id"]
    assert after[0]["text"] == before["text"]
    assert after[0]["source_item_ids"] == [first["id"], second["id"]]
    assert any(detail["source_item_id"] == second["id"] and "12345" in detail["text"] for detail in after[0]["details"])
    assert after[0]["due_on"] == before["due_on"]
    assert after[0]["deadline_reference_at"] == before["deadline_reference_at"]
    invalidate(incremental, second["id"])
    analyze(incremental)
    assert tasks(incremental)[0]["details"] == after[0]["details"]


@pytest.mark.parametrize("later", [
    "Pay water bill today",
    "Pay electricity bill tomorrow",
    "Pay electricity bill every month",
])
def test_related_tasks_and_separate_occurrences_do_not_merge(incremental, later):
    capture(incremental)
    analyze(incremental)
    original = tasks(incremental)[0]
    new = capture(incremental, later)
    analyze(incremental)
    rows = tasks(incremental)
    retained = next(row for row in rows if row["id"] == original["id"])
    assert retained == original
    assert any(new["id"] in row["source_item_ids"] for row in rows if row["id"] != original["id"])


def test_same_relative_wording_captured_later_is_a_separate_occurrence(incremental):
    capture(incremental)
    analyze(incremental)
    original = tasks(incremental)[0]
    incremental.clock.instant = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    new = capture(incremental)
    analyze(incremental)
    rows = tasks(incremental)
    assert len(rows) == 2, rows
    assert next(row for row in rows if row["id"] == original["id"]) == original
    occurrence = next(row for row in rows if row["id"] != original["id"])
    assert occurrence["source_item_ids"] == [new["id"]]
    assert occurrence["due_on"] == "2026-10-20"


def test_completed_undated_obligation_does_not_absorb_a_new_occurrence(incremental):
    capture(incremental, "Buy milk")
    analyze(incremental)
    original = tasks(incremental)[0]
    incremental.client.post(f"/api/tasks/{original['id']}/complete")
    saved = tasks(incremental)[0]
    incoming = capture(incremental, "Buy milk")
    analyze(incremental)
    rows = tasks(incremental)
    assert next(row for row in rows if row["id"] == saved["id"]) == saved
    assert any(row["id"] != saved["id"] and incoming["id"] in row["source_item_ids"] for row in rows)


def test_ambiguous_match_is_reviewable_and_does_not_overwrite_candidate(incremental):
    capture(incremental, "Pay electricity bill amount 2000 today")
    analyze(incremental)
    before = tasks(incremental)[0]
    incoming = capture(incremental, "Pay electricity bill amount 2100 today")
    analyze(incremental)
    after = next(row for row in tasks(incremental) if row["id"] == before["id"])
    assert after == before
    response = incremental.client.get("/api/analysis/reviews")
    assert response.status_code == 200, response.text
    reviews = response.json()
    review = next(row for row in reviews if row["item_id"] == incoming["id"])
    assert review["candidate_task_id"] == before["id"]
    assert review["status"] == "pending"
    assert review["reason"]
    resolved = incremental.client.post(
        f"/api/analysis/reviews/{review['id']}/resolve", json={"decision": "keep_separate"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] != "pending"
    assert next(row for row in tasks(incremental) if row["id"] == before["id"]) == before
    assert analyze(incremental)["metrics"]["target_count"] == 0


@pytest.mark.parametrize("choice", ["confirmed", "edited", "cleared"])
def test_attaching_a_source_preserves_completion_and_explicit_deadline(incremental, choice):
    capture(incremental, "Pay invoice ELEC-123 today")
    analyze(incremental)
    task = tasks(incremental)[0]
    if choice == "confirmed":
        response = incremental.client.post(f"/api/tasks/{task['id']}/confirm-timing", json={})
    else:
        response = incremental.client.patch(
            f"/api/tasks/{task['id']}/deadline",
            json={"due_on": "2026-10-25" if choice == "edited" else None},
        )
    assert response.status_code == 200, response.text
    incremental.client.post(f"/api/tasks/{task['id']}/complete")
    before = tasks(incremental)[0]
    incoming = capture(incremental, "Invoice ELEC-123 account number is 12345")
    analyze(incremental)
    after = tasks(incremental)
    assert len(after) == 1
    for field in ("id", "status", "text", "priority", "due_on", "deadline_status", "deadline_reference_at", "deadline_timezone"):
        assert after[0][field] == before[field]
    assert incoming["id"] in after[0]["source_item_ids"]


def test_analysis_state_context_and_proposals_are_owner_scoped(incremental, monkeypatch):
    response = incremental.client.post("/api/auth/dev-login", json={"email": "owner-a@example.com"})
    assert response.status_code == 200, response.text
    first = capture(incremental, "Pay private ALPHA electricity bill today")
    analyze(incremental)
    saved_a = tasks(incremental)
    b_client = incremental.new_client()
    response = b_client.post("/api/auth/dev-login", json={"email": "owner-b@example.com"})
    assert response.status_code == 200, response.text
    b = SimpleNamespace(client=b_client, main=incremental.main, db=incremental.db)
    incoming = capture(b, "Pay private BETA electricity bill today")
    assert b.client.post(f"/api/items/{first['id']}/invalidate-analysis").status_code == 404
    assert first["id"] not in states(b)
    planner = incremental.main.propose_delta
    captured = []

    def forge_foreign_task(batch):
        captured.append(repr(batch))
        result = planner(batch)
        result["plan"]["changes"] = [{
            "proposal_id": "foreign", "operation": "attach_source",
            "source_item_ids": [incoming["id"]], "task_id": saved_a[0]["id"],
            "reason": "Invalid foreign task reference must be rejected.",
        }]
        return result

    monkeypatch.setattr(incremental.main, "propose_delta", forge_foreign_task)
    result = b.client.post("/api/analyze")
    assert result.status_code == 500, result.text
    assert captured and "ALPHA" not in captured[0]
    assert tasks(b) == []
    assert tasks(incremental) == saved_a
    assert incoming["id"] not in states(incremental)
    assert incremental.client.get("/api/analysis/reviews").json() == []


def test_unknown_reference_rejects_the_whole_proposal_transaction(incremental, monkeypatch):
    incoming = capture(incremental)
    planner = incremental.main.propose_delta

    def forge_missing(batch):
        result = planner(batch)
        result["plan"]["changes"].append({
            "proposal_id": "missing", "operation": "attach_source",
            "source_item_ids": [incoming["id"]], "task_id": 999999,
            "reason": "Missing stable ID cannot be applied.",
        })
        return result

    monkeypatch.setattr(incremental.main, "propose_delta", forge_missing)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 500, response.text
    assert tasks(incremental) == []
    assert incremental.client.get(f"/api/items/{incoming['id']}/insight").json()["analysis_pending"] is True
    with incremental.db.SessionLocal() as session:
        assert session.get(incremental.db.ItemInsightRecord, incoming["id"]) is None


def test_owned_task_outside_selected_context_is_not_model_authority(incremental, monkeypatch):
    capture(incremental, "Pay electricity bill today")
    analyze(incremental)
    original = tasks(incremental)[0]
    incoming = capture(incremental, "Buy gardening gloves")
    planner = incremental.main.propose_delta

    def forge_unseen(batch):
        assert original["id"] not in {row["id"] for row in batch["supporting_context"]["tasks"]}
        result = planner(batch)
        result["plan"]["changes"] = [{
            "proposal_id": "unseen", "operation": "attach_source",
            "source_item_ids": [incoming["id"]], "task_id": original["id"],
            "reason": "Ownership alone does not authorize an unseen reference.",
        }]
        return result

    monkeypatch.setattr(incremental.main, "propose_delta", forge_unseen)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 500, response.text
    assert tasks(incremental) == [original]


def test_model_field_updates_cannot_overwrite_saved_deadline_or_status(incremental, monkeypatch):
    capture(incremental)
    analyze(incremental)
    original = tasks(incremental)[0]
    incoming = capture(incremental)
    planner = incremental.main.propose_delta

    def forge_fields(batch):
        result = planner(batch)
        result["plan"]["changes"] = [{
            "proposal_id": "unsafe_fields", "operation": "update_task",
            "source_item_ids": [incoming["id"]], "task_id": original["id"],
            "fields": {"status": "completed", "due_on": "2026-12-25"},
            "reason": "A model cannot overwrite protected saved fields.",
        }]
        return result

    monkeypatch.setattr(incremental.main, "propose_delta", forge_fields)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 500, response.text
    assert tasks(incremental) == [original]


@pytest.mark.parametrize("measured", [None, 321])
def test_provider_token_usage_is_reported_separately_from_estimates(incremental, monkeypatch, measured):
    capture(incremental)
    from app.analysis.planner import fallback_delta

    planner = incremental.main.propose_delta
    observed = {}

    def observe(batch):
        observed["batch"] = batch
        return planner(batch)

    def create(**kwargs):
        observed.setdefault("calls", []).append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(fallback_delta(observed["batch"])),
            usage=SimpleNamespace(input_tokens=measured),
        )

    monkeypatch.setattr(incremental.main, "propose_delta", observe)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-never-used-on-network")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(
        OpenAI=lambda: SimpleNamespace(responses=SimpleNamespace(create=create)),
    ))
    result = analyze(incremental)
    assert result["mode"] == "openai"
    assert len(observed["calls"]) == 1
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["input_tokens"] == measured
    assert result["metrics"]["estimated_input_tokens"] > 0
    again = analyze(incremental)
    assert len(observed["calls"]) == 1
    assert again["metrics"]["model_calls"] == 0


def test_lookup_requests_are_targeted_bounded_and_make_at_most_one_extra_call(incremental, monkeypatch):
    capture(incremental, "Pay invoice ELEC-123 today")
    analyze(incremental)
    capture(incremental, "Invoice ELEC-123 account number is 12345")
    planner = incremental.main.propose_delta
    batches = []

    def request_once(batch):
        batches.append(copy.deepcopy(batch))
        result = planner(batch)
        result["metrics"]["model_calls"] = 1
        result["metrics"]["input_tokens"] = 50
        if len(batches) == 1:
            result["plan"]["changes"] = []
            result["plan"]["lookup_requests"] = [{"query": "invoice ELEC-123"}]
        return result

    monkeypatch.setattr(incremental.main, "propose_delta", request_once)
    result = analyze(incremental)
    assert len(batches) == 2
    assert result["metrics"]["lookup_count"] == 1
    assert result["metrics"]["model_calls"] == 2
    assert result["metrics"]["input_tokens"] == 100
    assert result["metrics"]["context_task_count"] <= 8
    assert result["metrics"]["context_snippet_count"] <= 8
    assert result["metrics"]["context_characters"] <= 10000
    assert batches[0]["targets"] == batches[1]["targets"]


def test_unresolved_lookup_is_blocked_without_applying_partial_changes(incremental, monkeypatch):
    capture(incremental)
    planner = incremental.main.propose_delta
    batches = []

    def still_insufficient(batch):
        batches.append(batch)
        output = planner(batch)
        output["plan"]["lookup_requests"] = [{"query": "missing invoice 123"}]
        return output

    monkeypatch.setattr(incremental.main, "propose_delta", still_insufficient)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 409, response.text
    assert len(batches) == 2
    assert tasks(incremental) == []
    assert next(iter(states(incremental).values()))["status"] == "blocked"
    forbid_planner(monkeypatch, incremental)
    assert analyze(incremental)["metrics"]["model_calls"] == 0


def test_lookup_preserves_evidence_that_context_is_still_incomplete(incremental, monkeypatch):
    for index in range(9):
        capture(incremental, f"Pay invoice SHARED-999 amount {index + 100} today")
    analyze(incremental)
    analyze(incremental)
    before = tasks(incremental)
    capture(incremental, "Pay invoice SHARED-999 amount 999 today")
    planner = incremental.main.propose_delta
    batches = []

    def observe(batch):
        batches.append(batch)
        return planner(batch)

    monkeypatch.setattr(incremental.main, "propose_delta", observe)
    response = incremental.client.post("/api/analyze")
    assert response.status_code == 409, response.text
    assert len(batches) == 2
    assert batches[1]["supporting_context"]["metrics"]["candidate_count"] > (
        len(batches[1]["supporting_context"]["tasks"]) + len(batches[1]["supporting_context"]["snippets"]))
    assert tasks(incremental) == before


def test_complete_negative_reference_lookup_does_not_require_entire_inbox(incremental):
    for index in range(8):
        capture(incremental, f"Pay invoice OLD-{index + 100} today")
    analyze(incremental)
    incoming = capture(incremental, "Pay invoice NEW-999 today")
    result = analyze(incremental)
    assert result["metrics"]["lookup_count"] == 1
    assert result["metrics"]["target_ids"] == [incoming["id"]]
    assert result["metrics"]["context_task_count"] <= 8
    assert result["metrics"]["context_characters"] <= 10000
    assert len(tasks(incremental)) == 9


def test_two_occurrences_in_one_new_source_keep_separate_saved_dates(incremental):
    capture(incremental, "Pay electricity bill today. Pay electricity bill tomorrow.")
    analyze(incremental)
    rows = tasks(incremental)
    assert len(rows) == 2
    assert {row["due_on"] for row in rows} == {"2026-10-15", "2026-10-16"}
    assert analyze(incremental)["metrics"]["target_count"] == 0
    assert tasks(incremental) == rows


def test_added_sibling_occurrence_does_not_rewrite_saved_occurrence(incremental):
    item = capture(incremental)
    analyze(incremental)
    original = tasks(incremental)[0]
    incremental.clock.instant = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    response = incremental.client.put(f"/api/items/{item['id']}", json={
        "raw_text": "Pay electricity bill tomorrow. Pay electricity bill today.",
    })
    assert response.status_code == 200, response.text
    analyze(incremental)
    rows = tasks(incremental)
    assert len(rows) == 2
    saved = next(row for row in rows if row["id"] == original["id"])
    assert saved["due_on"] == original["due_on"] == "2026-10-15"
    added = next(row for row in rows if row["id"] != original["id"])
    # The newly introduced timing phrase has the October 20 edit reference;
    # the existing "today" obligation keeps its October 15 reference.
    assert added["due_on"] == "2026-10-21"
    assert added["deadline_reference_at"].startswith("2026-10-20")


def test_new_provenance_does_not_acknowledge_an_unreviewed_source_edit(incremental):
    item = capture(incremental, "Pay invoice ELEC-123 amount 2000 today")
    analyze(incremental)
    task = tasks(incremental)[0]
    response = incremental.client.patch(f"/api/tasks/{task['id']}/deadline", json={"due_on": "2026-10-25"})
    assert response.status_code == 200
    incremental.clock.instant = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    response = incremental.client.put(f"/api/items/{item['id']}", json={
        "raw_text": "Pay invoice ELEC-123 amount 2100 today",
    })
    assert response.status_code == 200
    added = capture(incremental, "Invoice ELEC-123 account number is 12345")
    analyze(incremental)
    saved = tasks(incremental)
    assert len(saved) == 1
    assert saved[0]["id"] == task["id"]
    assert saved[0]["due_on"] == "2026-10-25"
    assert saved[0]["source_item_ids"] == [item["id"], added["id"]]
    assert saved[0]["source_changed"] is True


def test_simultaneous_runs_claim_each_target_once(incremental, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    capture(incremental)
    # Seed state before testing the atomic claim, as a background poll would.
    states(incremental)
    entered, release = Event(), Event()
    planner = incremental.main.propose_delta
    calls = []

    def slow_planner(batch):
        calls.append(batch)
        entered.set()
        assert release.wait(10)
        return planner(batch)

    monkeypatch.setattr(incremental.main, "propose_delta", slow_planner)
    other_client = incremental.new_client()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(incremental.client.post, "/api/analyze")
        try:
            assert entered.wait(10)
            second = other_client.post("/api/analyze")
            assert second.status_code == 200, second.text
            assert second.json()["metrics"]["target_count"] == 0
        finally:
            release.set()
        completed = first.result(timeout=10)
    assert completed.status_code == 200, completed.text
    assert len(calls) == 1
    assert len(tasks(incremental)) == 1
