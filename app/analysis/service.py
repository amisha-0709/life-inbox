"""One bounded, recoverable delta for manual or background organization callers."""
from copy import deepcopy
import importlib
import json

from app.analysis.context import select_context
from app.analysis.contracts import validate_plan
from app.analysis.fingerprints import CONTRACT_VERSION


def run_incremental_analysis(propose):
    # Resolve on each invocation: tests/reloads use isolated engines, and no
    # service-level cache may keep a previous owner's session or database alive.
    db = importlib.import_module("app.database")
    run_id = db.start_analysis_run()
    claim = None
    try:
        claim = db.prepare_incremental_batch(run_id)
        targets = claim["targets"]
        metrics = {"target_count": len(targets), "target_ids": [row["id"] for row in targets],
                   "target_characters": sum(len(row["text"]) for row in targets),
                   "context_task_count": 0, "context_snippet_count": 0, "context_characters": 0,
                   "model_calls": 0, "input_tokens": 0, "estimated_input_tokens": 0,
                   "serialized_input_characters": 0, "lookup_count": 0, "noop": not bool(targets)}
        context = {"tasks": [], "snippets": []}
        if not targets:
            return db.apply_incremental_delta(run_id, claim,
                {"organizations": [], "changes": [], "lookup_requests": []}, context, metrics,
                "empty_inbox" if not db.list_items_from_db(limit=1) else "fallback_no_api_call")

        candidates, snippets = db.analysis_context_candidates()
        context = select_context(targets, candidates, snippets)
        batch = {"contract_version": CONTRACT_VERSION, "targets": targets, "supporting_context": context,
                 "budgets": {"context_max_tasks": 6, "context_max_snippets": 6, "context_max_characters": 8000,
                             "max_lookup_rounds": 1, "max_target_count": 8, "max_target_characters": 48000}}
        mode = "fallback_no_api_call"
        for round_index in range(2):
            output = propose(deepcopy(batch))
            plan = validate_plan(output["plan"], targets, context)
            usage = output.get("metrics", {})
            calls = usage.get("model_calls", 0)
            metrics["model_calls"] += calls
            metrics["estimated_input_tokens"] += usage.get("input_token_estimate", 0) if calls else 0
            metrics["serialized_input_characters"] += usage.get("serialized_input_characters", 0) if calls else 0
            if calls:
                measured = usage.get("input_tokens")
                metrics["input_tokens"] = (metrics["input_tokens"] + measured
                    if metrics["input_tokens"] is not None and measured is not None else None)
            mode = output["mode"]
            if not plan["lookup_requests"]:
                break
            if round_index == 1:
                db.release_incremental_claim(claim, "Additional targeted context is still required. Explicitly retry after reviewing the source.", blocked=True)
                raise ContextBudgetExhausted("Context remains insufficient after the bounded targeted lookup; items require review.")
            metrics["lookup_count"] += 1
            candidate_count = context["metrics"].get("candidate_count", 0)
            task_candidate_count = context["metrics"].get("candidate_task_count", 0)
            snippet_candidate_count = context["metrics"].get("candidate_snippet_count", 0)
            truncated = context["metrics"].get("truncated", False)
            context["lookup_results"] = []
            # Keep prior context snapshots so concurrent corrections cannot be
            # silently accepted as if the model had seen their new values.
            for request in plan["lookup_requests"]:
                lookup = {"query": request["query"], "complete": False}
                context["lookup_results"].append(lookup)
                if len(context["tasks"]) >= 8 and len(context["snippets"]) >= 8:
                    break
                room = 10000 - len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) - 200
                if room < 500:
                    break
                extra = select_context(targets, candidates, snippets, request=request,
                    excluded_task_ids={row["id"] for row in context["tasks"]},
                    excluded_item_ids={row["id"] for row in context["snippets"]},
                    max_tasks=8-len(context["tasks"]), max_snippets=8-len(context["snippets"]), max_chars=room)
                lookup["complete"] = (not extra["metrics"]["truncated"]
                                      and not any(row.get("text_truncated") for row in context["tasks"] + context["snippets"]))
                candidate_count = max(candidate_count, len(context["tasks"]) + len(context["snippets"])
                                      + extra["metrics"]["candidate_count"])
                task_candidate_count = max(task_candidate_count, len(context["tasks"]) + extra["metrics"]["candidate_task_count"])
                snippet_candidate_count = max(snippet_candidate_count, len(context["snippets"]) + extra["metrics"]["candidate_snippet_count"])
                truncated |= extra["metrics"]["truncated"]
                context["tasks"].extend(extra["tasks"])
                context["snippets"].extend(extra["snippets"])
            context["metrics"] = {"task_count": len(context["tasks"]), "snippet_count": len(context["snippets"]),
                                  "candidate_count": candidate_count, "truncated": truncated}
            context["metrics"].update(candidate_task_count=task_candidate_count,
                                      candidate_snippet_count=snippet_candidate_count)
            context["budgets"] = {"max_tasks": 8, "max_snippets": 8, "max_chars": 10000}
            batch["budgets"].update(context_max_tasks=8, context_max_snippets=8, context_max_characters=10000)
            if len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) > 10000:
                raise ValueError("Targeted lookup exceeded the total context budget")
        metrics.update(context_task_count=len(context["tasks"]), context_snippet_count=len(context["snippets"]),
                       context_characters=len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))))
        return db.apply_incremental_delta(run_id, claim, plan, context, metrics, mode)
    except Exception as exc:
        if claim is not None and not isinstance(exc, ContextBudgetExhausted):
            db.release_incremental_claim(claim, "Analysis failed; explicitly retry the item.")
        db.fail_analysis_run(run_id)
        raise


class ContextBudgetExhausted(ValueError):
    pass
