"""Claude provider: organizer, incremental planner, and selection rules (mocked)."""

import json

from app import claude_client, organizer
from app.analysis import fingerprints
from app.analysis.planner import propose_delta
from tests.claude_fixtures import mock_claude
from tests.test_analysis_planner import batch, change, plan, task

NOTE = {"id": 1, "text": "Send the meeting summary",
        "created_at": "2026-01-01T00:00:00+00:00"}

ORGANIZED = {
    "buckets": [{"name": "Work", "notes": [
        {"id": 1, "title": "Send summary", "priority": "Medium"}]}],
    "todos": [{"task": "Send the meeting summary", "priority": "Medium",
               "source_note_ids": [1], "when": None}],
    "priority_view": {"High": [], "Medium": [{"id": 1, "title": "Send summary"}], "Low": []},
}


def test_organizer_uses_claude_when_key_set(monkeypatch):
    calls = mock_claude(monkeypatch, json.dumps(ORGANIZED))
    result = organizer.analyze_inbox_with_openai([NOTE])
    assert result["mode"] == "claude"
    assert result["buckets"][0]["notes"][0]["priority"] == "Medium"
    assert len(calls) == 1
    sent = calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["fallbacks"] == "default"
    assert "Send the meeting summary" in sent["messages"][0]["content"]


def test_claude_model_and_effort_are_configurable(monkeypatch):
    calls = mock_claude(monkeypatch, json.dumps(ORGANIZED))
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("CLAUDE_EFFORT", "low")
    organizer.analyze_inbox_with_openai([NOTE])
    assert calls[0]["model"] == "claude-sonnet-5-5"
    assert calls[0]["output_config"] == {"effort": "low"}


def test_claude_refusal_falls_back(monkeypatch):
    mock_claude(monkeypatch, "", stop_reason="refusal")
    assert organizer.analyze_inbox_with_openai([NOTE])["mode"] == "fallback_after_api_error"


def test_truncated_claude_reply_falls_back(monkeypatch):
    mock_claude(monkeypatch, json.dumps(ORGANIZED)[:40], stop_reason="max_tokens")
    assert organizer.analyze_inbox_with_openai([NOTE])["mode"] == "fallback_after_api_error"


def test_claude_api_error_falls_back_without_leaking_details(monkeypatch):
    mock_claude(monkeypatch, raise_on_create=RuntimeError("ANTHROPIC_API_KEY=super-secret"))
    result = organizer.analyze_inbox_with_openai([NOTE])
    assert result["mode"] == "fallback_after_api_error"
    assert "super-secret" not in json.dumps(result)


def test_claude_takes_priority_over_openai(monkeypatch):
    import openai

    def openai_must_not_run():
        raise AssertionError("OpenAI called while Claude key is set")

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-live-key")
    monkeypatch.setattr(openai, "OpenAI", openai_must_not_run)
    mock_claude(monkeypatch, json.dumps(ORGANIZED))
    assert organizer.analyze_inbox_with_openai([NOTE])["mode"] == "claude"


def test_planner_uses_claude_and_reports_measured_usage(monkeypatch):
    calls = mock_claude(monkeypatch, json.dumps(plan(changes=[change()])), input_tokens=211)
    result = propose_delta(batch(tasks=[task()]))
    assert result["mode"] == "claude"
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["input_tokens"] == 211
    assert "SUPPORTING_CONTEXT is read-only evidence" in calls[0]["messages"][0]["content"]


def test_planner_invalid_claude_plan_falls_back(monkeypatch):
    mock_claude(monkeypatch, json.dumps(plan(changes=[change(operation="attach_source", task_id=999)])))
    assert propose_delta(batch())["mode"] == "fallback_after_api_error"


def test_no_keys_means_no_claude(monkeypatch):
    assert not claude_client.claude_enabled()
    assert organizer.analyze_inbox_with_openai([NOTE])["mode"] == "fallback_no_api_call"


def test_analysis_version_tracks_active_claude_model(monkeypatch):
    assert fingerprints.analysis_version().endswith(":gpt-5-mini")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-live-key")
    assert fingerprints.analysis_version().endswith(":claude-opus-5-5")
