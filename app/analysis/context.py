"""Bounded, deterministic supporting context and conservative obligation matching.

Callers must supply owner-scoped candidates. This module never queries storage and
never treats context as an analysis target. Character budgets count the complete
JSON payload returned by :func:`select_context`, including its budget metadata.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import re
import unicodedata

from app.attention.dates import reference_date


_STOP_WORDS = frozenset(
    "a an and are as at be been by can could do for from had has have i in is it "
    "me my of on or our please should that the their them then there these they "
    "this to was we were will with would you your today tomorrow yesterday due "
    "urgent high medium low task note reminder".split()
)
_TOKEN = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_REFERENCE = re.compile(
    r"\b(invoice|reference|ref|order|booking|ticket|account|receipt)"
    r"\s*(?:number|no\.?|id)?\s*[:#-]?\s*([a-z0-9]+(?:[-/_][a-z0-9]+)*)\b",
    re.I,
)
_RECURRENCE = re.compile(
    r"\b(?:every|each)\s+\w+|\b(?:daily|weekly|fortnightly|monthly|quarterly|"
    r"yearly|annually|annual|recurring|repeat(?:ing)?)\b",
    re.I,
)
_UNCERTAIN = re.compile(r"\b(?:maybe|perhaps|possibly|not|instead|except|rather than)\b", re.I)
_TASK_FIELDS = (
    "id", "text", "priority", "ai_text", "ai_priority", "user_text", "user_priority",
    "has_user_overrides", "status", "due_on", "timing_text",
    "deadline_status", "deadline_needs_review", "deadline_reference_at",
    "deadline_timezone", "source_item_ids", "corrections", "details",
    "source_fingerprint", "reference_at", "timezone", "occurrence_id", "source_text",
)
_SNIPPET_FIELDS = (
    "id", "text", "item_type", "reference_at", "timezone", "corrections",
    "input_fingerprint", "source_url",
)


def _normal(text: object) -> str:
    return " ".join(_TOKEN.findall(unicodedata.normalize("NFKC", str(text or "")).casefold()))


def _tokens(text: object) -> set[str]:
    return {word for word in _normal(text).split() if len(word) > 1 and word not in _STOP_WORDS}


def _references(text: object) -> set[str]:
    # A label such as "invoice tomorrow" is not a stable identifier.
    return {match.group(2).casefold() for match in _REFERENCE.finditer(str(text or ""))
            if any(character.isdigit() for character in match.group(2))}


def _typed_references(text: str, *, include_accounts: bool = True) -> set[tuple[str, str]]:
    """Keep reference namespaces for precise lookups and identity checks."""
    result = set()
    for match in _REFERENCE.finditer(text):
        kind, identifier = match.group(1).casefold(), match.group(2).casefold()
        if (kind == "account" and not include_accounts) or not any(character.isdigit() for character in identifier):
            continue
        result.add(("reference" if kind == "ref" else kind, identifier))
    return result


def _identity_references(text: str) -> set[tuple[str, str]]:
    """Account numbers identify reusable destinations, never an obligation."""
    return _typed_references(text, include_accounts=False)


def _combined_text(value: dict) -> str:
    return " ".join(str(value.get(key) or "") for key in ("text", "task", "source_text", "timing_text"))


def _reference_day(value: dict):
    return reference_date(
        value.get("reference_at") or value.get("deadline_reference_at"),
        value.get("timezone") or value.get("deadline_timezone"),
    )


def _known_date(value: object):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _action_head(action: str) -> str:
    words = [word for word in action.split()
             if word not in _STOP_WORDS | {"need", "remember", "must", "remind"}]
    return words[0] if words else ""


def _reference_action_core(text: str) -> str:
    """Remove a narrow payment-detail suffix, preserving vendor/action objects.

    Matching an invoice number does not establish that a deposit and a balance,
    or invoices from different vendors, are the same obligation. Additional
    account metadata is safe only when the remaining action wording agrees.
    """
    text = re.sub(
        r"\s+(?:using|via|with|from)\s+(?:bank\s+)?account\s*"
        r"(?:(?:number|no\.?|id)\s*)?[:#-]?\s*[a-z0-9]+(?:[-/_][a-z0-9]+)*[.!]?\s*$",
        "", text, flags=re.I,
    )
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = _REFERENCE.sub(
        lambda match: f"{'reference' if match.group(1).casefold() == 'ref' else match.group(1).casefold()} {match.group(2).casefold()}",
        text,
    )
    return _normal(text)


def _numbers(text: str) -> set[str]:
    """Identify conflicting amounts/details without confusing comma formatting."""
    # Invoice/reference numbers establish identity; they are not numeric detail.
    # Otherwise invoice 999 would hide amount 999 -> 100 because the identifier
    # supplies the old amount to both sets. Reusable account numbers stay as
    # details so conflicting payment destinations still require review.
    text = _REFERENCE.sub(lambda match: match.group() if match.group(1).casefold() == "account" else "", text)
    return {number.replace(",", "").lstrip("0") or "0"
            for number in re.findall(r"(?<!\w)\d[\d,]*(?:\.\d+)?(?!\w)", text)}


def equivalent_obligations(left: dict, right: dict) -> str:
    """Return equivalent, possible_duplicate, related, or distinct.

    Exact wording alone is deliberately insufficient: equal, known occurrence
    dates and capture days are required. A stable invoice/reference identifier
    plus an equal occurrence date permits a recognized account-detail suffix
    when the base action agrees and numeric facts do not conflict. Recurring
    captures require a shared explicit occurrence identifier.
    This is a guard for automatic attachment, not a claim of semantic identity.
    """
    left_text, right_text = _combined_text(left), _combined_text(right)
    left_raw_action = str(left.get("text") or left.get("task") or "")
    right_raw_action = str(right.get("text") or right.get("task") or "")
    left_action = _normal(left_raw_action)
    right_action = _normal(right_raw_action)
    if not left_action or not right_action:
        return "distinct"
    left_date, right_date = _known_date(left.get("due_on")), _known_date(right.get("due_on"))
    left_occurrence, right_occurrence = left.get("occurrence_id"), right.get("occurrence_id")
    if left_occurrence and right_occurrence and left_occurrence != right_occurrence:
        return "distinct"
    if left_date and right_date and left_date != right_date:
        return "distinct"
    shared_occurrence = bool(left_occurrence and left_occurrence == right_occurrence)
    recurring = bool(_RECURRENCE.search(left_text) or _RECURRENCE.search(right_text))
    if recurring and not shared_occurrence:
        return "distinct"
    same_date = bool(left_date and left_date == right_date)
    same_capture_day = bool(_reference_day(left) and _reference_day(left) == _reference_day(right))
    exact = left_action == right_action
    # A capture may contain several obligations. Whole-source references or
    # amounts belong to provenance; they cannot prove this particular action is
    # the same invoice/occurrence as another task. Full source wording only adds
    # conservative recurrence/uncertainty guards above and below.
    left_references = _identity_references(left_raw_action)
    right_references = _identity_references(right_raw_action)
    if left_references and right_references and not left_references & right_references:
        return "distinct"
    common_references = left_references & right_references
    left_numbers, right_numbers = _numbers(left_raw_action), _numbers(right_raw_action)
    numeric_compatible = left_numbers <= right_numbers or right_numbers <= left_numbers
    uncertain = bool(left.get("deadline_needs_review") or right.get("deadline_needs_review")
                     or _UNCERTAIN.search(left_text) or _UNCERTAIN.search(right_text))
    if not uncertain and (same_date or shared_occurrence):
        if exact and (same_capture_day or shared_occurrence):
            return "equivalent"
        if (common_references and numeric_compatible
                and _action_head(left_action) == _action_head(right_action)
                and _reference_action_core(left_raw_action) == _reference_action_core(right_raw_action)):
            return "equivalent"
    if exact or common_references:
        return "possible_duplicate"
    overlap = _tokens(left_action) & _tokens(right_action)
    # One generic word such as "bill" is too weak even for a review suggestion.
    if len(overlap) >= 2:
        return "related"
    return "distinct"


def _clip(text: object, limit: int) -> tuple[str, bool]:
    value = str(text or "")
    return (value, False) if len(value) <= limit else (value[:limit - 1] + "…", True)


def _compact(candidate: dict, *, task: bool, target_ids: set) -> tuple[dict, bool]:
    fields = _TASK_FIELDS if task else _SNIPPET_FIELDS
    result = {key: deepcopy(candidate[key]) for key in fields if key in candidate}
    if task and "text" not in result and "task" in candidate:
        result["text"] = candidate["task"]
    result["text"], clipped = _clip(result.get("text"), 240 if task else 600)
    if result.get("ai_text"):
        result["ai_text"], ai_clipped = _clip(result["ai_text"], 240)
        clipped |= ai_clipped
    if result.get("source_text"):
        result["source_text"], source_clipped = _clip(result["source_text"], 600)
        clipped |= source_clipped
    if task and len(result.get("source_item_ids") or ()) > 12:
        source_ids = result["source_item_ids"]
        # Keep links to targets even when an obligation has many older sources.
        linked = [source_id for source_id in source_ids if source_id in target_ids]
        others = [source_id for source_id in source_ids if source_id not in target_ids]
        result["source_item_ids"] = (linked + others)[:12]
        result["source_item_count"] = len(source_ids)
        result["source_ids_truncated"] = True
        clipped = True
    if clipped:
        result["text_truncated"] = True
    return result, clipped


def _serialize(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _measure(payload: dict) -> int:
    # The digit count of this field can itself change serialized length.
    for _ in range(8):
        actual = len(_serialize(payload))
        if payload["metrics"]["characters"] == actual:
            return actual
        payload["metrics"]["characters"] = actual
    return len(_serialize(payload))


def select_context(
    targets: list[dict],
    tasks: list[dict],
    snippets: list[dict],
    *,
    request: dict | None = None,
    excluded_task_ids: set | None = None,
    excluded_item_ids: set | None = None,
    max_tasks: int = 6,
    max_snippets: int = 6,
    max_chars: int = 8000,
) -> dict:
    """Select relevant context from already owner-scoped candidates.

    A lookup request may contain ``query``, ``task_ids``, and/or ``item_ids``.
    Explicit IDs retrieve only those IDs; a query additionally retrieves matching
    candidates. A query naming a stable reference requires that typed reference,
    rather than generic keyword overlap. Empty lookups return no records. Limits
    apply to lookups too.
    Oversized records are omitted rather than clipping authoritative corrections.
    ``ValueError`` indicates that even the empty metadata envelope cannot fit.
    """
    if min(max_tasks, max_snippets, max_chars) < 0:
        raise ValueError("Context budgets must be non-negative.")
    excluded_tasks = set(excluded_task_ids or ())
    excluded_items = set(excluded_item_ids or ()) | {target["id"] for target in targets}
    target_ids = {target["id"] for target in targets}
    target_text = " ".join(_combined_text(target) for target in targets)
    query = str((request or {}).get("query") or "") if request is not None else target_text
    query_tokens, query_references = _tokens(query), _references(query)
    lookup_references = _typed_references(query) if request is not None else set()
    requested_tasks = set((request or {}).get("task_ids") or ())
    requested_items = set((request or {}).get("item_ids") or ())
    ranked = []
    seen = set()
    for is_task, candidates in ((True, tasks), (False, snippets)):
        excluded = excluded_tasks if is_task else excluded_items
        requested = requested_tasks if is_task else requested_items
        for candidate in candidates:
            candidate_id = candidate.get("id")
            key = (is_task, candidate_id)
            if candidate_id is None or candidate_id in excluded or key in seen:
                continue
            seen.add(key)
            text = _combined_text(candidate)
            overlap = query_tokens & _tokens(text)
            references = query_references & _references(text)
            linked = bool(is_task and target_ids & set(candidate.get("source_item_ids") or ()))
            explicit = candidate_id in requested
            if request is not None:
                # Do not fall back to the original targets for an empty lookup.
                targeted_match = (lookup_references & _typed_references(text)
                                  if lookup_references else references or overlap)
                relevant = explicit or bool(query and targeted_match)
            else:
                relevant = linked or bool(references or overlap)
            if not relevant:
                continue
            score = 10000 * explicit + 1000 * linked + 100 * len(references) + 5 * len(overlap)
            compact, clipped = _compact(candidate, task=is_task, target_ids=target_ids)
            ranked.append((score, str(candidate_id), is_task, compact, clipped))
    ranked.sort(key=lambda row: (-row[0], row[1], not row[2]))
    result = {
        "tasks": [], "snippets": [],
        "metrics": {"task_count": 0, "snippet_count": 0, "characters": 0,
                    "truncated": False, "candidate_count": len(ranked),
                    "candidate_task_count": sum(row[2] for row in ranked),
                    "candidate_snippet_count": sum(not row[2] for row in ranked)},
        "budgets": {"max_tasks": max_tasks, "max_snippets": max_snippets, "max_chars": max_chars},
    }
    if _measure(result) > max_chars:
        raise ValueError("Context character budget cannot hold its metadata envelope.")
    for _, _, is_task, compact, clipped in ranked:
        group, count_key, maximum = ("tasks", "task_count", max_tasks) if is_task else ("snippets", "snippet_count", max_snippets)
        if len(result[group]) >= maximum:
            result["metrics"]["truncated"] = True
            continue
        result[group].append(compact)
        result["metrics"][count_key] += 1
        if clipped:
            result["metrics"]["truncated"] = True
        if _measure(result) > max_chars:
            result[group].pop()
            result["metrics"][count_key] -= 1
            result["metrics"]["truncated"] = True
    # Marking truncation changes JSON length (false vs true); always remeasure.
    _measure(result)
    return result
