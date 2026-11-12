"""Focused tests for bounded supporting context and occurrence-safe matching."""

import json

import pytest

from app.analysis.context import equivalent_obligations, select_context


def obligation(text="Pay electricity bill today", **fields):
    return {
        "id": 1, "text": text, "due_on": "2026-10-15", "status": "pending",
        "reference_at": "2026-10-15T10:00:00+00:00", "timezone": "Asia/Kolkata",
        **fields,
    }


def serialized(context):
    return json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_selects_relevant_bounded_context_with_stable_metadata():
    target = {"id": 100, "text": "Pay invoice ELEC-123 today"}
    tasks = [obligation(f"Pay invoice ELEC-{index} today", id=index) for index in range(20)]
    matching = obligation(
        "Pay invoice ELEC-123 today", id=123, source_item_ids=[45],
        deadline_status="user_edited", corrections={"due_on": "2026-10-15"},
        details=[{"text": "Use account 99881", "source_item_ids": [45]}],
    )
    context = select_context([target], tasks + [matching], [], max_tasks=3)
    assert len(context["tasks"]) == 3
    assert context["tasks"][0]["id"] == 123
    assert context["tasks"][0]["source_item_ids"] == [45]
    assert context["tasks"][0]["corrections"] == {"due_on": "2026-10-15"}
    assert context["tasks"][0]["deadline_status"] == "user_edited"
    assert context["metrics"]["candidate_count"] == 21
    assert context["metrics"]["candidate_task_count"] == 21
    assert context["metrics"]["candidate_snippet_count"] == 0
    assert context["metrics"]["truncated"] is True
    assert context["metrics"]["characters"] == len(serialized(context))


def test_source_link_ranked_before_keyword_or_reference_matches():
    target = {"id": 100, "text": "Pay invoice ELEC-123 today"}
    context = select_context(
        [target],
        [obligation("Completely revised wording", id=1, source_item_ids=[100]),
         obligation("Pay invoice ELEC-123 today", id=2, source_item_ids=[99])],
        [], max_tasks=1,
    )
    assert [task["id"] for task in context["tasks"]] == [1]


def test_many_provenance_links_keep_target_links_and_report_omissions():
    context = select_context(
        [{"id": 100, "text": "Completely revised text"}],
        [obligation(source_item_ids=list(range(20)) + [100])], [],
    )
    task = context["tasks"][0]
    assert len(task["source_item_ids"]) == 12
    assert task["source_item_ids"][0] == 100
    assert task["source_item_count"] == 21
    assert task["source_ids_truncated"] is True


def test_unrelated_context_and_target_snippets_are_excluded():
    context = select_context(
        [{"id": 100, "text": "Pay electricity bill today"}],
        [obligation("Read gardening manual", id=20)],
        [{"id": 100, "text": "Pay electricity bill today"},
         {"id": 21, "text": "Book theatre tickets"}],
    )
    assert context["tasks"] == context["snippets"] == []
    assert context["metrics"]["candidate_count"] == 0


def test_candidate_metrics_distinguish_task_and_snippet_omissions():
    context = select_context(
        [{"id": 100, "text": "Electricity bill"}], [],
        [{"id": index, "text": "Electricity bill source"} for index in range(12)],
        max_snippets=2,
    )
    assert context["metrics"]["candidate_count"] == 12
    assert context["metrics"]["candidate_task_count"] == 0
    assert context["metrics"]["candidate_snippet_count"] == 12
    assert context["metrics"]["snippet_count"] == 2
    assert context["metrics"]["truncated"] is True
    assert context["metrics"]["characters"] == len(serialized(context))


def test_lookup_is_targeted_and_does_not_repeat_previous_context():
    targets = [{"id": 100, "text": "Pay electricity bill today"}]
    tasks = [obligation(id=1), obligation("Read gardening manual", id=2)]
    snippets = [{"id": 20, "text": "Gardening reference"}]
    context = select_context(
        targets, tasks, snippets, request={"task_ids": [2], "item_ids": [20]},
    )
    assert [task["id"] for task in context["tasks"]] == [2]
    assert [item["id"] for item in context["snippets"]] == [20]
    repeated = select_context(
        targets, tasks, snippets, request={"query": "gardening", "task_ids": [2]},
        excluded_task_ids={2}, excluded_item_ids={20},
    )
    assert repeated["tasks"] == repeated["snippets"] == []
    empty = select_context(targets, tasks, snippets, request={})
    assert empty["tasks"] == empty["snippets"] == []


def test_lookup_cannot_exceed_context_limits_or_return_unknown_ids():
    tasks = [obligation(id=index) for index in range(12)]
    context = select_context([], tasks, [], request={"task_ids": list(range(20))}, max_tasks=2)
    assert len(context["tasks"]) == 2
    assert context["metrics"]["candidate_count"] == 12
    assert context["metrics"]["truncated"] is True


def test_reference_lookup_does_not_admit_generic_invoice_keyword_matches():
    tasks = [obligation(f"Pay invoice INV-{index} today", id=index) for index in range(20)]
    tasks.append(obligation("Pay invoice INV-123 today", id=123))
    snippets = [{"id": 40, "text": "Invoice INV-456 supporting details"},
                {"id": 41, "text": "Invoice INV-123 supporting details"}]
    context = select_context([], tasks, snippets, request={"query": "invoice INV-123"})
    assert [task["id"] for task in context["tasks"]] == [123]
    assert [item["id"] for item in context["snippets"]] == [41]
    assert context["metrics"]["candidate_count"] == 2
    assert context["metrics"]["truncated"] is False


def test_reference_lookup_proves_absence_without_filling_with_other_invoices():
    context = select_context(
        [], [obligation(f"Pay invoice INV-{index} today", id=index) for index in range(20)],
        [], request={"query": "invoice INV-123"},
    )
    assert context["tasks"] == []
    assert context["metrics"]["candidate_count"] == 0
    assert context["metrics"]["truncated"] is False


def test_reference_lookup_preserves_namespace_and_explicit_id_requests():
    context = select_context(
        [], [obligation("Pay invoice 123 today", id=1),
             obligation("Pay ticket 123 today", id=2),
             obligation("Pay invoice 999 today", id=3)],
        [], request={"query": "invoice 123", "task_ids": [3]},
    )
    assert {task["id"] for task in context["tasks"]} == {1, 3}


def test_reference_lookup_supports_aliases_and_reusable_account_retrieval():
    tasks = [obligation("Pay reference INV-123 today", id=1),
             obligation("Use account 123", id=2),
             obligation("Pay invoice 123 today", id=3)]
    alias = select_context([], tasks, [], request={"query": "ref INV-123"})
    account = select_context([], tasks, [], request={"query": "account 123"})
    assert [task["id"] for task in alias["tasks"]] == [1]
    assert [task["id"] for task in account["tasks"]] == [2]


def test_initial_retrieval_remains_broader_than_exact_reference_lookup():
    context = select_context(
        [{"id": 100, "text": "Pay invoice INV-123 today"}],
        [obligation("Pay invoice INV-456 today", id=1)], [],
    )
    assert [task["id"] for task in context["tasks"]] == [1]


def test_complete_json_budget_includes_metadata_and_unicode():
    tasks = [obligation("Pay electricity bill ₹2,000 " + "detail " * 100, id=index)
             for index in range(12)]
    snippets = [{"id": 30 + index, "text": "Electricity bill " + "detail " * 200}
                for index in range(12)]
    context = select_context(
        [{"id": 100, "text": "Electricity bill"}], tasks, snippets, max_chars=1600,
    )
    assert len(serialized(context)) <= 1600
    assert context["metrics"]["characters"] == len(serialized(context))
    assert context["metrics"]["truncated"] is True
    assert all(len(task["text"]) <= 240 for task in context["tasks"])
    assert all(len(item["text"]) <= 600 for item in context["snippets"])
    assert tasks[0]["text"].endswith("detail " * 100)  # No caller mutation.


def test_oversized_corrections_are_omitted_instead_of_silently_changed():
    huge = obligation(id=1, corrections={"text": "Do not lose this correction. " * 500})
    small = obligation(id=2, corrections={"due_on": None})
    context = select_context([{"id": 100, "text": "Electricity bill"}], [huge, small], [], max_chars=1000)
    assert [task["id"] for task in context["tasks"]] == [2]
    assert context["tasks"][0]["corrections"] == {"due_on": None}
    assert context["metrics"]["truncated"] is True
    assert context["metrics"]["characters"] == len(serialized(context))


def test_context_uses_explicit_safe_fields_and_clips_full_source_text():
    candidate = obligation(
        source_text="Electricity bill source " * 200,
        source_snippets=[{"text": "Private full source " * 500}],
        unrelated_internal_data="Do not send to organizer",
    )
    context = select_context([{"id": 100, "text": "Electricity bill"}], [candidate], [])
    task = context["tasks"][0]
    assert len(task["source_text"]) == 600
    assert task["text_truncated"] is True
    assert "source_snippets" not in task
    assert "unrelated_internal_data" not in task


def test_context_is_deterministic_and_deduplicates_candidate_ids():
    candidates = [obligation(id=3), obligation(id=1), obligation(id=2), obligation(id=2)]
    targets = [{"id": 100, "text": "Electricity bill"}]
    first = select_context(targets, candidates, [])
    second = select_context(targets, list(reversed(candidates)), [])
    assert first == second
    assert [task["id"] for task in first["tasks"]] == [1, 2, 3]


@pytest.mark.parametrize("limits", [{"max_chars": 10}, {"max_tasks": -1}, {"max_snippets": -1}])
def test_invalid_or_unusable_budgets_are_explicit(limits):
    with pytest.raises(ValueError):
        select_context([], [], [], **limits)


def test_identical_known_occurrence_can_be_automatically_attached():
    assert equivalent_obligations(obligation(), obligation(id=2)) == "equivalent"
    assert equivalent_obligations(obligation(), obligation(id=2, status="completed")) == "equivalent"


@pytest.mark.parametrize("left_fields,right_fields", [
    ({"due_on": None}, {"due_on": None}),
    ({"reference_at": None}, {}),
    ({"timezone": None}, {}),
    ({"deadline_needs_review": True}, {}),
    ({"due_on": None, "status": "completed"}, {"due_on": None}),
])
def test_unknown_or_uncertain_occurrence_stays_reviewable(left_fields, right_fields):
    assert equivalent_obligations(obligation(**left_fields), obligation(**right_fields)) == "possible_duplicate"


def test_different_capture_day_never_reinterprets_relative_wording_as_equivalent():
    later = obligation(reference_at="2026-10-20T10:00:00+00:00")
    assert equivalent_obligations(obligation(), later) == "possible_duplicate"


def test_capture_day_uses_each_recorded_timezone_across_midnight():
    first = obligation(reference_at="2026-10-14T23:30:00+00:00", timezone="Asia/Kolkata")
    same_day = obligation(reference_at="2026-10-15T01:00:00+00:00", timezone="UTC")
    prior_day = obligation(reference_at="2026-10-14T23:30:00+00:00", timezone="UTC")
    assert equivalent_obligations(first, same_day) == "equivalent"
    assert equivalent_obligations(first, prior_day) == "possible_duplicate"


def test_stable_reference_allows_safe_additional_detail_without_overwriting_task():
    original = obligation("Pay invoice ELEC-123 today")
    enriched = obligation("Pay invoice ELEC-123 today using account 99881")
    assert equivalent_obligations(original, enriched) == "equivalent"
    conflicting = obligation("Pay invoice ELEC-123 amount ₹2,100 today")
    previous = obligation("Pay invoice ELEC-123 amount ₹2,000 today")
    assert equivalent_obligations(previous, conflicting) == "possible_duplicate"
    assert equivalent_obligations(
        obligation("Pay Acme invoice ELEC-123 today"),
        obligation("Pay Acme invoice ELEC-123 today using account 99881"),
    ) == "equivalent"


def test_reference_digits_cannot_hide_a_conflicting_numeric_detail():
    original = obligation("Pay invoice SHARED-999 amount 999 today")
    conflicting = obligation("Pay invoice SHARED-999 amount 100 today")
    assert equivalent_obligations(original, conflicting) == "possible_duplicate"
    assert equivalent_obligations(conflicting, original) == "possible_duplicate"


def test_numeric_detail_comparison_preserves_amount_formatting_and_new_accounts():
    original = obligation("Pay invoice SHARED-999 amount ₹2,000 today")
    enriched = obligation("Pay invoice SHARED-999 amount ₹2000 today using account 12345")
    assert equivalent_obligations(original, enriched) == "equivalent"
    changed_account = obligation("Pay invoice SHARED-999 amount ₹2000 today using account 98765")
    assert equivalent_obligations(enriched, changed_account) == "possible_duplicate"


def test_shared_invoice_identifier_does_not_combine_distinct_actions():
    original = obligation("Pay invoice ELEC-123 today")
    assert equivalent_obligations(original, obligation("Dispute invoice ELEC-123 today")) == "possible_duplicate"
    assert equivalent_obligations(original, obligation("Do not pay invoice ELEC-123 today")) == "possible_duplicate"


@pytest.mark.parametrize("left,right", [
    ("Pay Acme invoice 123 today", "Pay Globex invoice 123 today"),
    ("Pay invoice 123 deposit today", "Pay invoice 123 balance today"),
    ("Pay invoice 123 today", "Pay invoice 123 today using account 12345 to pay rent"),
])
def test_shared_reference_cannot_hide_different_vendors_or_obligation_objects(left, right):
    assert equivalent_obligations(obligation(left), obligation(right)) == "possible_duplicate"
    assert equivalent_obligations(obligation(right), obligation(left)) == "possible_duplicate"


@pytest.mark.parametrize("left,right", [
    ("Pay invoice 2026/123 today", "Pay invoice 2026/456 today"),
    ("Pay invoice 2026_123 today", "Pay invoice 2026_456 today"),
    ("Pay invoice 2026/123 today", "Pay invoice 2026_123 today"),
])
def test_complete_reference_identifiers_survive_action_normalization(left, right):
    assert equivalent_obligations(obligation(left), obligation(right)) == "distinct"


@pytest.mark.parametrize("other_kind", ["ticket", "order", "booking", "receipt", "reference"])
def test_reference_namespaces_cannot_supply_false_identity(other_kind):
    original = obligation("Pay invoice 123 today")
    different = obligation(f"Pay {other_kind} 123 today")
    assert equivalent_obligations(original, different) != "equivalent"


def test_reusable_account_id_does_not_establish_obligation_identity():
    electricity = obligation("Pay electricity bill account 123 today")
    water = obligation("Pay water bill account 123 today")
    assert equivalent_obligations(electricity, water) == "related"
    assert equivalent_obligations(water, electricity) == "related"
    enriched = obligation("Pay electricity bill account 123 today using bank transfer")
    assert equivalent_obligations(electricity, enriched) != "equivalent"


def test_account_id_still_helps_targeted_context_retrieval():
    context = select_context(
        [{"id": 100, "text": "Pay electricity bill account 123 today"}],
        [obligation("Payment destination account 123", id=20)], [],
    )
    assert [task["id"] for task in context["tasks"]] == [20]


def test_reference_abbreviations_keep_the_same_explicit_namespace():
    assert equivalent_obligations(
        obligation("Pay reference INV-123 today"),
        obligation("Pay ref INV-123 today using account 99881"),
    ) == "equivalent"


def test_other_obligations_in_full_source_cannot_supply_matching_identifiers():
    source = "Pay invoice INV-123 today. Pay rent today."
    existing_invoice = obligation("Pay invoice INV-123 today", source_text=source)
    sibling_rent = obligation("Pay rent today", source_text=source)
    assert equivalent_obligations(existing_invoice, sibling_rent) == "distinct"
    assert equivalent_obligations(sibling_rent, existing_invoice) == "distinct"


def test_other_obligation_amounts_cannot_hide_conflicting_task_specific_amounts():
    source = "Pay invoice INV-123 ₹2,000 today. Save ₹2,100 today."
    original = obligation("Pay invoice INV-123 ₹2,000 today", source_text=source)
    conflicting = obligation("Pay invoice INV-123 ₹2,100 today", source_text=source)
    assert equivalent_obligations(original, conflicting) == "possible_duplicate"


def test_malformed_date_is_not_evidence_of_a_known_occurrence():
    assert equivalent_obligations(
        obligation(due_on="2026-02-31"), obligation(due_on="2026-02-31"),
    ) == "possible_duplicate"


@pytest.mark.parametrize("changed", [
    {"due_on": "2026-10-16"}, {"occurrence_id": "november"},
])
def test_distinct_occurrences_remain_separate(changed):
    original = obligation("Pay invoice ELEC-123", occurrence_id="october")
    assert equivalent_obligations(original, obligation("Pay invoice ELEC-123", **changed)) == "distinct"


@pytest.mark.parametrize("wording", [
    "Pay electricity bill every month", "Pay electricity bill monthly",
    "Pay recurring electricity bill", "Pay electricity bill each Monday",
])
def test_recurring_wording_does_not_establish_a_duplicate_occurrence(wording):
    assert equivalent_obligations(obligation(wording), obligation(wording)) == "distinct"
    assert equivalent_obligations(
        obligation(wording, occurrence_id="2026-10"),
        obligation(wording, occurrence_id="2026-10"),
    ) == "equivalent"


def test_source_wording_retains_recurrence_even_when_short_task_title_omits_it():
    recurring = obligation(source_text="Pay electricity bill every month")
    assert equivalent_obligations(recurring, obligation()) == "distinct"


def test_related_but_distinct_actions_are_not_automatically_combined():
    assert equivalent_obligations(
        obligation("Pay electricity bill today"), obligation("Review electricity bill today"),
    ) == "related"
    assert equivalent_obligations(
        obligation("Pay electricity bill today"), obligation("Pay water bill today"),
    ) == "related"
    assert equivalent_obligations(
        obligation("Pay electricity bill today"), obligation("Book dentist appointment today"),
    ) == "distinct"
