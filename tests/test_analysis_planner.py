"""The organizer sees a bounded delta and cannot authorize global rewrites."""

import json
from types import SimpleNamespace

import pytest

from app.analysis.contracts import validate_plan
from app.analysis.planner import fallback_delta, propose_delta, same_reference_detail


def target(item_id=1, text="Pay invoice INV-123 today", **overrides):
    return {"id": item_id, "text": text, "item_type": "text", "input_fingerprint": f"fp-{item_id}",
            "reference_at": "2026-10-15T07:00:00+00:00", "timezone": "Asia/Kolkata",
            "created_at": "2026-10-15T07:00:00+00:00", **overrides}


def task(task_id=41, text="Pay invoice INV-123 today", **overrides):
    return {"id": task_id, "text": text, "priority": "High", "status": "open",
            "timing_text": "today", "due_on": "2026-10-15", "deadline_status": "inferred",
            "deadline_needs_review": False, "source_item_ids": [99],
            "deadline_reference_at": "2026-10-15T07:00:00+00:00",
            "deadline_timezone": "Asia/Kolkata", "corrections": {}, "details": [], **overrides}


def batch(targets=None, tasks=None, **context):
    return {"contract_version": "delta-v1", "targets": targets if targets is not None else [target()],
            "supporting_context": {"tasks": tasks or [], "snippets": [], **context}, "budgets": {}}


def plan(organizations=None, changes=None, lookup_requests=None):
    return {"organizations": organizations if organizations is not None else [
        {"item_id": 1, "title": "Pay invoice", "category": "Money & Admin", "priority": "High"}],
            "changes": changes or [], "lookup_requests": lookup_requests or []}


def change(**overrides):
    return {"proposal_id": "p1", "operation": "create_task", "source_item_ids": [1],
            "text": "Pay invoice", "reason": "Explicit source obligation", **overrides}


def test_contract_organizes_only_and_all_targets():
    with pytest.raises(ValueError, match="each analysis target"):
        validate_plan(plan(organizations=[]), [target()], {})
    with pytest.raises(ValueError, match="each analysis target"):
        validate_plan(plan(organizations=[plan()["organizations"][0]] * 2), [target()], {})
    with pytest.raises(ValueError, match="extra_forbidden"):
        validate_plan({**plan(), "replace_all_tasks": []}, [target()], {})


@pytest.mark.parametrize("proposal", [
    change(source_item_ids=[2]),
    change(operation="attach_source", task_id=999),
    change(operation="attach_source", existing_proposal_id="future"),
    change(operation="update_task", task_id=41, text=None, fields={"status": "completed"}),
    change(operation="update_task", task_id=41, text=None, fields={"due_on": "2026-10-20"}),
    change(operation="update_task", task_id=41, text=None, fields={"text": "rewritten"}),
    change(operation="create_task", task_id=41),
    change(operation="flag_related", task_id=41),
    change(operation="attach_source", task_id=41, existing_proposal_id="p0"),
    change(source_item_ids=[1, 1]),
])
def test_contract_rejects_unknown_ids_and_authoritative_rewrites(proposal):
    with pytest.raises(ValueError):
        validate_plan(plan(changes=[proposal]), [target()], {"tasks": [task()]})


def test_contract_allows_only_backward_local_create_references_and_sourced_details():
    proposals = [change(), change(proposal_id="p2", operation="attach_source",
                                 existing_proposal_id="p1", text=None),
                 change(proposal_id="p3", operation="update_task", task_id=41, text=None,
                        fields={"detail": "Shipping is included."})]
    assert len(validate_plan(plan(changes=proposals), [target()], {"tasks": [task()]})["changes"]) == 3
    with pytest.raises(ValueError, match="unique"):
        validate_plan(plan(changes=[change(), change()]), [target()], {})
    with pytest.raises(ValueError, match="backward"):
        validate_plan(plan(changes=[proposals[1], proposals[0]]), [target()], {})


def test_fallback_creates_only_target_obligation():
    result = fallback_delta(batch(tasks=[task(text="Book dentist appointment tomorrow")]))
    assert [row["item_id"] for row in result["organizations"]] == [1]
    creates = [row for row in result["changes"] if row["operation"] == "create_task"]
    assert [row["source_item_ids"] for row in creates] == [[1]]
    assert creates[0]["timing_text"] == "today"


def test_fallback_equivalent_existing_occurrence_attaches_provenance():
    result = fallback_delta(batch(tasks=[task()]))
    assert [row["operation"] for row in result["changes"]] == ["attach_source"]
    assert result["changes"][0]["task_id"] == 41
    assert result["changes"][0]["source_item_ids"] == [1]


def test_fallback_same_source_amount_or_timing_edits_attach_existing_task():
    for text in ["Pay the ₹2,100 electricity bill today", "Pay the ₹2,000 electricity bill tomorrow"]:
        result = fallback_delta(batch([target(text=text)], [task(text="Pay the ₹2,000 electricity bill today", source_item_ids=[1])]))
        assert result["changes"][0]["operation"] == "attach_source"
        assert result["changes"][0]["task_id"] == 41
        assert result["changes"][0]["timing_text"] in {"today", "tomorrow"}


@pytest.mark.parametrize("old_text,new_text", [
    ("Pay invoice INV-123 today", "Pay invoice INV-124 today"),
    ("Call the dentist today", "Call the doctor today"),
    ("Pay invoice INV-123 today", "Research invoice INV-123 today"),
])
def test_source_linkage_does_not_combine_a_changed_obligation(old_text, new_text):
    result = fallback_delta(batch([target(text=new_text)], [task(text=old_text, source_item_ids=[1])]))
    assert result["changes"][0]["operation"] == "create_task"


def test_same_source_typo_is_an_edit_to_existing_obligation():
    result = fallback_delta(batch([target(text="Pay the electricity bill today")],
                                  [task(text="Pay the electrcity bill today", source_item_ids=[1])]))
    assert result["changes"][0]["operation"] == "attach_source"


def test_fallback_checks_equivalent_occurrences_inside_new_batch():
    result = fallback_delta(batch([target(), target(2)]))
    assert [row["operation"] for row in result["changes"]] == ["create_task", "attach_source"]
    assert result["changes"][1]["existing_proposal_id"] == result["changes"][0]["proposal_id"]
    assert result["changes"][1]["source_item_ids"] == [2]


def test_separate_obligation_does_not_inherit_reference_from_another_sentence():
    result = fallback_delta(batch([target(text="Pay invoice INV-123 today. Pay rent today.")], [task()]))
    assert result["changes"][0]["operation"] == "attach_source"
    creates = [row for row in result["changes"] if row["operation"] == "create_task"]
    assert [row["text"] for row in creates] == ["Pay rent today"]


def test_distinct_occurrences_in_one_new_source_are_not_treated_as_source_edits():
    result = fallback_delta(batch([target(text="Pay electricity bill today. Pay electricity bill tomorrow.")]))
    assert [row["operation"] for row in result["changes"]] == ["create_task", "create_task"]
    assert [row["timing_text"] for row in result["changes"]] == ["today", "tomorrow"]


def test_added_occurrence_uses_source_edit_privilege_only_once():
    result = fallback_delta(batch([target(text="Pay electricity bill tomorrow. Pay electricity bill today.")],
                                  [task(text="Pay electricity bill today", source_item_ids=[1])]))
    attachments = [row for row in result["changes"] if row["operation"] == "attach_source"]
    creates = [row for row in result["changes"] if row["operation"] == "create_task"]
    assert len(attachments) == len(creates) == 1
    assert attachments[0]["task_id"] == 41
    assert attachments[0]["timing_text"] == "today"
    assert creates[0]["timing_text"] == "tomorrow"


def test_multiple_linked_candidates_are_reviewed_instead_of_arbitrarily_selected():
    result = fallback_delta(batch([target(text="Pay electricity bill amount 2200 today")], [
        task(41, text="Pay electricity bill amount 2000 today", source_item_ids=[1]),
        task(42, text="Pay electricity bill amount 2100 today", source_item_ids=[1]),
    ]))
    assert not any(row["operation"] == "attach_source" for row in result["changes"])
    assert any(row["operation"] == "flag_possible_duplicate" for row in result["changes"])


def test_fallback_ambiguous_undated_capture_keeps_separate_task_and_review():
    result = fallback_delta(batch([target(text="Call the dentist")], [task(text="Call the dentist", due_on=None, timing_text=None)]))
    assert [row["operation"] for row in result["changes"]] == ["create_task", "flag_possible_duplicate"]
    assert result["changes"][1]["task_id"] == 41


@pytest.mark.parametrize("old_text,new_text,old_due", [
    ("Pay rent every month", "Pay rent every month", None),
    ("Pay invoice INV-123 today", "Pay invoice INV-123 tomorrow", "2026-10-15"),
    ("Call the dentist", "Call the dentist", None),
])
def test_fallback_keeps_recurring_different_date_and_completed_undated_occurrences_separate(old_text, new_text, old_due):
    result = fallback_delta(batch([target(text=new_text)], [task(text=old_text, due_on=old_due, status="completed")]))
    assert result["changes"][0]["operation"] == "create_task"
    assert not any(row["operation"] == "attach_source" for row in result["changes"])


def test_fallback_related_distinct_tasks_keep_identities():
    result = fallback_delta(batch([target(text="Call dentist to discuss invoice INV-123 today")],
                                  [task(text="Pay dentist invoice INV-456 today")]))
    assert result["changes"][0]["operation"] == "create_task"
    assert not any(row["operation"] == "attach_source" for row in result["changes"])


def test_fallback_stable_reference_detail_enriches_without_task_rewrite():
    result = fallback_delta(batch([target(text="Invoice INV-123 includes ₹100 shipping")], [task()]))
    assert [row["operation"] for row in result["changes"]] == ["attach_source", "update_task"]
    assert result["changes"][1]["fields"] == {"detail": "Invoice INV-123 includes ₹100 shipping"}
    assert result["changes"][1]["text"] is None
    assert result["changes"][1]["timing_text"] is None


@pytest.mark.parametrize("text", [
    "Invoice INV-456 includes shipping", "Invoice INV-123 and invoice INV-456 include shipping",
    "Invoice INV-123 is due tomorrow", "Invoice INV-123 is monthly", "Invoice INV-123 was cancelled",
    "Pay invoice INV-123", "Invoice INV-123 is not correct",
])
def test_detail_guard_rejects_ambiguous_or_new_action_timing(text):
    assert not same_reference_detail({"text": text}, task())


def test_detail_guard_never_uses_a_sibling_source_reference():
    assert not same_reference_detail({"text": "Invoice INV-123 includes shipping"},
                                     task(text="Pay rent today", source_text="Pay invoice INV-123 today. Pay rent today."))


def test_detail_guard_preserves_complete_slash_identifiers():
    assert not same_reference_detail({"text": "Invoice 2026/123 includes shipping"}, task(text="Pay invoice 2026/456 today"))
    assert same_reference_detail({"text": "Invoice 2026/123 includes shipping"}, task(text="Pay invoice 2026/123 today"))


def test_detail_guard_rejects_a_different_vendor_with_same_invoice_number():
    assert not same_reference_detail({"text": "Globex invoice 123 includes shipping"},
                                     task(text="Pay Acme invoice 123 today"))


def test_non_action_detail_with_multiple_candidates_requires_review():
    result = fallback_delta(batch([target(text="Invoice INV-123 includes shipping")], [
        task(41, text="Pay Acme invoice INV-123 today"),
        task(42, text="Pay Globex invoice INV-123 today"),
    ]))
    assert [row["operation"] for row in result["changes"]] == ["flag_possible_duplicate"] * 2


def test_fallback_missing_legacy_reference_does_not_use_current_clock():
    result = fallback_delta(batch([target(reference_at=None, timezone=None)], [task()]))
    assert result["changes"][0]["operation"] == "create_task"


def test_omitted_context_for_unmatched_reference_requests_targeted_lookup():
    result = fallback_delta(batch(tasks=[task(text="Pay unrelated invoice INV-456 today")],
                                  metrics={"candidate_count": 10, "truncated": True}))
    assert result["lookup_requests"] == [{"query": "invoice inv-123", "task_ids": [], "item_ids": []}]


def test_source_text_clipping_alone_does_not_request_another_lookup():
    result = fallback_delta(batch(tasks=[task(text="Pay unrelated invoice INV-456 today")],
                                  metrics={"candidate_count": 1, "truncated": True}))
    assert result["lookup_requests"] == []


def test_no_key_and_empty_targets_make_zero_model_calls(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = propose_delta(batch())
    assert result["mode"] == "fallback_no_api_call"
    assert result["metrics"]["model_calls"] == 0
    assert result["metrics"]["input_tokens"] is None
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-live-key")
    result = propose_delta(batch([]))
    assert result["plan"] == {"organizations": [], "changes": [], "lookup_requests": []}
    assert result["metrics"] == {"model_calls": 0, "input_tokens": None, "input_token_estimate": 0, "serialized_input_characters": 0}


def fake_model(monkeypatch, output, tokens=123):
    import openai
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(output_text=json.dumps(output), usage=SimpleNamespace(input_tokens=tokens))

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-live-key")
    monkeypatch.setattr(openai, "OpenAI", lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    return calls


def test_model_prompt_separates_targets_support_and_reports_measured_usage(monkeypatch):
    calls = fake_model(monkeypatch, plan(changes=[change()]), tokens=137)
    result = propose_delta(batch(tasks=[task()]))
    assert result["mode"] == "openai"
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["input_tokens"] == 137
    assert result["metrics"]["input_token_estimate"] > 0
    assert "SUPPORTING_CONTEXT is read-only evidence, NOT additional targets" in calls[0]["input"]
    assert '"source_item_ids":[99]' in calls[0]["input"]
    assert len(calls) == 1


def test_model_may_request_targeted_context_without_global_ids(monkeypatch):
    calls = fake_model(monkeypatch, plan(lookup_requests=[{"query": "invoice INV-123", "task_ids": [], "item_ids": []}]))
    result = propose_delta(batch())
    assert result["mode"] == "openai"
    assert result["plan"]["lookup_requests"][0]["query"] == "invoice INV-123"
    assert len(calls) == 1


def test_invalid_model_reference_falls_back_without_losing_measured_usage(monkeypatch):
    fake_model(monkeypatch, plan(changes=[change(operation="attach_source", task_id=999)]), tokens=321)
    result = propose_delta(batch())
    assert result["mode"] == "fallback_after_api_error"
    assert result["metrics"]["input_tokens"] == 321
    assert result["metrics"]["model_calls"] == 1
    assert result["plan"]["changes"][0]["operation"] == "create_task"


def test_model_failure_counts_attempt_but_does_not_invent_measured_tokens(monkeypatch):
    fake_model(monkeypatch, RuntimeError("mock unavailable"))
    result = propose_delta(batch())
    assert result["mode"] == "fallback_after_api_error"
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["input_tokens"] is None


@pytest.mark.parametrize("oversized", [
    batch([target(i + 1) for i in range(9)]),
    batch([target(text="x" * 24001)]),
    batch(tasks=[task(i + 1) for i in range(7)]),
    batch(snippets=[{"id": i + 1, "text": "x"} for i in range(7)]),
    batch(tasks=[task(text="x" * 8001)]),
    {**batch(), "existing_tasks": [task()]},
    batch([target(title="generated presentation field")]),
])
def test_budget_violation_fails_before_any_model_call(monkeypatch, oversized):
    calls = fake_model(monkeypatch, plan())
    with pytest.raises(ValueError):
        propose_delta(oversized)
    assert calls == []


def test_expanded_lookup_budget_is_explicit_and_still_hard_bounded(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    expanded = batch(tasks=[task(i + 1, text="Different reference") for i in range(8)])
    expanded["budgets"] = {"context_max_tasks": 8, "context_max_snippets": 8,
                           "context_max_characters": 10000}
    assert propose_delta(expanded)["metrics"]["model_calls"] == 0
    expanded["supporting_context"]["tasks"].append(task(9))
    expanded["budgets"]["context_max_tasks"] = 1000
    with pytest.raises(ValueError, match="record budget"):
        propose_delta(expanded)
