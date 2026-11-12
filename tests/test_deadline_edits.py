"""Source content edits must not silently renew an existing relative deadline."""

import pytest

from tests.test_deadlines import inbox, at, capture, analyze, tasks, only_task, preview


def edit(inbox, item, text):
    at(inbox, "2026-10-20T10:00:00+00:00")
    response = inbox.client.put(
        f"/api/items/{item['id']}", json={"raw_text": text},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        ("Pay the ₹2,000 electricity bill today", "Pay the ₹2,100 electricity bill today"),
        ("Pay electricity bill today, amount ₹2,000", "Pay electricity bill today, amount ₹2,100"),
        ("Pay the electricity biil today", "Pay the electricity bill today"),
        ("Call Smiht today", "Call Smith today"),
        ("Today pay the ₹2,000 electricity bill", "Today pay the ₹2,100 electricity bill"),
        ("Pay electricity bill today", "Pay electricity bill today. Do not lose the receipt"),
    ],
)
def test_unrelated_edit_keeps_deadline_and_reference(inbox, original, changed):
    item = capture(inbox, original)
    analyze(inbox)
    before = only_task(inbox)
    assert before["due_on"] == "2026-10-15"
    edit(inbox, item, changed)
    analyze(inbox)
    after = only_task(inbox)
    assert after["id"] == before["id"]
    assert after["due_on"] == before["due_on"]
    assert after["deadline_reference_at"] == before["deadline_reference_at"]
    assert after["deadline_timezone"] == before["deadline_timezone"]
    assert after["deadline_needs_review"] is False
    assert after["source_changed"] is False
    board = inbox.client.get("/api/attention").json()
    assert after["id"] in {row["id"] for row in board["overdue"]}
    assert preview(inbox, after["id"]).json()["start_date"] == "2026-10-15"
    analyze(inbox)
    restarted = inbox.restart()
    assert restarted.client.get("/api/tasks?status=all").json()[0]["due_on"] == "2026-10-15"


def test_unrelated_edit_before_first_analysis_keeps_capture_reference(inbox):
    item = capture(inbox, "Pay the ₹2,000 electricity bill today")
    changed = edit(inbox, item, "Pay the ₹2,100 electricity bill today")
    assert changed["time_reference_at"] == item["time_reference_at"]
    analyze(inbox)
    assert only_task(inbox)["due_on"] == "2026-10-15"


@pytest.mark.parametrize("changed_sibling", ["Call the dentist today", "Call dentist tomorrow"])
def test_editing_other_obligation_does_not_reanchor_bill(inbox, changed_sibling):
    item = capture(inbox, "Pay electricity bill today. Call dentist today")
    analyze(inbox)
    before = {row["text"].split()[0]: row for row in tasks(inbox)}
    edit(inbox, item, f"Pay electricity bill today. {changed_sibling}")
    analyze(inbox)
    after = {row["text"].split()[0]: row for row in tasks(inbox)}
    assert len(after) == 2
    assert after["Pay"]["id"] == before["Pay"]["id"]
    assert after["Pay"]["due_on"] == "2026-10-15"
    assert after["Pay"]["deadline_reference_at"] == before["Pay"]["deadline_reference_at"]
    assert after["Pay"]["deadline_needs_review"] is False
    assert after["Call"]["due_on"] == ("2026-10-21" if "tomorrow" in changed_sibling else "2026-10-15")


@pytest.mark.parametrize("wording", ["tomorrow", "in 0 days", "2026-10-25"])
def test_changed_timing_uses_edit_context(inbox, wording):
    item = capture(inbox)
    analyze(inbox)
    before = only_task(inbox)
    edit(inbox, item, f"Pay the electricity bill {wording}")
    analyze(inbox)
    after = only_task(inbox)
    assert after["id"] == before["id"]
    expected = {"tomorrow": "2026-10-21", "in 0 days": "2026-10-20", "2026-10-25": "2026-10-25"}
    assert after["due_on"] == expected[wording]
    assert after["deadline_reference_at"] == "2026-10-20T10:00:00+00:00"


@pytest.mark.parametrize("wording", ["", "maybe today", "before today", "after today", "not today", "no longer today", "tomorow", "tomorrow at 10 a.m."])
def test_ambiguous_timing_edit_retains_old_date_and_provenance_for_review(inbox, wording):
    item = capture(inbox)
    analyze(inbox)
    before = only_task(inbox)
    edit(inbox, item, f"Pay the electricity bill {wording}".strip())
    analyze(inbox)
    after = only_task(inbox)
    assert after["id"] == before["id"]
    assert after["due_on"] == "2026-10-15"
    assert after["deadline_reference_at"] == before["deadline_reference_at"]
    assert after["ai_timing_text"] == before["ai_timing_text"]
    assert after["deadline_needs_review"] is True
    assert after["source_changed"] is True
    assert preview(inbox, after["id"]).status_code == 400
    analyze(inbox)
    assert only_task(inbox)["deadline_needs_review"] is True


def test_ambiguous_obligation_match_keeps_both_prior_dates_for_review(inbox):
    item = capture(inbox, "Pay gas bill today. Pay water bill today")
    analyze(inbox)
    before = {row["id"]: row for row in tasks(inbox)}
    edit(inbox, item, "Pay bill tomorrow")
    analyze(inbox)
    after = tasks(inbox)
    assert {row["id"] for row in after} == set(before)
    for row in after:
        assert row["due_on"] == before[row["id"]]["due_on"]
        assert row["deadline_reference_at"] == before[row["id"]]["deadline_reference_at"]
        assert row["deadline_needs_review"] is True
        assert preview(inbox, row["id"]).status_code == 400


@pytest.mark.parametrize("original", ["Pay bill today. Call dentist today", "Pay bill today"])
def test_mixed_timing_edit_before_first_analysis_requires_review(inbox, original):
    item = capture(inbox, original)
    edit(inbox, item, "Pay bill today. Call dentist tomorrow")
    analyze(inbox)
    for row in tasks(inbox):
        assert row["due_on"] is None
        assert row["deadline_needs_review"] is True


def test_new_obligation_with_same_timing_in_an_edited_source_requires_review(inbox):
    item = capture(inbox, "Pay electricity bill today. Call dentist today")
    analyze(inbox)
    before_bill = next(row for row in tasks(inbox) if "electricity" in row["text"])
    edit(inbox, item, "Pay electricity bill today. Buy milk today")
    analyze(inbox)
    rows = tasks(inbox)
    bill = next(row for row in rows if "electricity" in row["text"])
    milk = next(row for row in rows if "milk" in row["text"])
    assert bill["id"] == before_bill["id"]
    assert bill["due_on"] == "2026-10-15"
    assert bill["deadline_needs_review"] is False
    assert milk["due_on"] is None
    assert milk["deadline_needs_review"] is True


def test_leading_timing_change_reuses_task_with_edit_context(inbox):
    item = capture(inbox, "Today pay electricity bill")
    analyze(inbox)
    before = only_task(inbox)
    edit(inbox, item, "Tomorrow pay electricity bill")
    analyze(inbox)
    after = only_task(inbox)
    assert after["id"] == before["id"]
    assert after["due_on"] == "2026-10-21"
    assert after["deadline_reference_at"] == "2026-10-20T10:00:00+00:00"


def test_extracted_text_amount_edit_preserves_deadline(inbox):
    from tests.pdf_fixtures import make_text_pdf

    created = inbox.client.post(
        "/api/items/pdf",
        files={"file": ("bill.pdf", make_text_pdf("Pay electricity bill today, amount 2000"), "application/pdf")},
        headers={"X-Life-Inbox-Timezone": "Asia/Kolkata"},
    ).json()
    analyze(inbox)
    before = only_task(inbox)
    at(inbox, "2026-10-20T10:00:00+00:00")
    updated = inbox.client.patch(
        f"/api/items/{created['id']}/extracted-text",
        json={"extracted_text": "Pay electricity bill today, amount 2100"},
        headers={"X-Life-Inbox-Timezone": "America/New_York"},
    )
    assert updated.status_code == 200, updated.text
    analyze(inbox)
    after = only_task(inbox)
    for field in ("id", "due_on", "deadline_reference_at", "deadline_timezone"):
        assert after[field] == before[field]
    assert preview(inbox, after["id"]).json()["start_date"] == "2026-10-15"
    analyze(inbox)
    assert only_task(inbox)["deadline_needs_review"] is False


@pytest.mark.parametrize("choice", ["confirmed", "edited", "cleared"])
@pytest.mark.parametrize("wording", ["Pay the ₹2,100 electricity bill today", "Pay the electricity bill tomorrow"])
def test_explicit_choices_survive_content_and_timing_edits(inbox, choice, wording):
    item = capture(inbox, "Pay the ₹2,000 electricity bill today")
    analyze(inbox)
    task = only_task(inbox)
    if choice == "confirmed":
        response = inbox.client.post(f"/api/tasks/{task['id']}/confirm-timing", json={})
    else:
        response = inbox.client.patch(
            f"/api/tasks/{task['id']}/deadline",
            json={"due_on": "2026-10-25" if choice == "edited" else None},
        )
    before = response.json()
    edit(inbox, item, wording)
    analyze(inbox)
    after = only_task(inbox)
    for field in ("id", "due_on", "deadline_status", "deadline_reference_at", "deadline_timezone"):
        assert after[field] == before[field]
    assert after["source_changed"] is True
