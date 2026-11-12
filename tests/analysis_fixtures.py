"""Adapt legacy organization fixtures to the production delta seam in tests only."""

from app.organizer import validate_analysis_result


def mock_analysis(monkeypatch, main, analyzer):
    def propose(batch):
        targets = batch["targets"]
        result = validate_analysis_result(analyzer(targets), targets)
        organizations = [
            {"item_id": note["id"], "title": note["title"], "category": bucket["name"],
             "priority": note["priority"], "tags": note.get("tags", []),
             "rationale": note.get("rationale") or ""}
            for bucket in result["buckets"] for note in bucket["notes"]
        ]
        changes = [
            {"proposal_id": f"fixture_{index}", "operation": "create_task",
             "source_item_ids": todo["source_note_ids"], "text": todo["task"],
             "priority": todo["priority"], "timing_text": todo.get("when"),
             "reason": "Mocked model proposes a task from explicit target sources."}
            for index, todo in enumerate(result["todos"])
        ]
        return {"plan": {"organizations": organizations, "changes": changes, "lookup_requests": []},
                "mode": result["mode"],
                "metrics": {"model_calls": 0, "input_tokens": None, "input_token_estimate": 0,
                            "serialized_input_characters": 0}}

    monkeypatch.setattr(main, "propose_delta", propose)
