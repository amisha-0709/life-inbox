"""The organizer proposes a delta; persisted records remain authoritative."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import Priority


class StrictDeltaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Organization(StrictDeltaModel):
    item_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=240)
    category: str = Field(min_length=1, max_length=100)
    priority: Priority
    tags: list[str] = Field(default_factory=list, max_length=12)
    rationale: str = Field(default="", max_length=1000)


class ProposedChange(StrictDeltaModel):
    proposal_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    operation: Literal[
        "create_task", "attach_source", "update_task",
        "flag_possible_duplicate", "flag_related",
    ]
    source_item_ids: list[int] = Field(min_length=1, max_length=8)
    task_id: int | None = Field(default=None, gt=0)
    existing_proposal_id: str | None = Field(default=None, max_length=80)
    text: str | None = Field(default=None, max_length=4000)
    priority: Priority = "Medium"
    timing_text: str | None = Field(default=None, max_length=2000)
    fields: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def appropriate_fields(self):
        if len(self.source_item_ids) != len(set(self.source_item_ids)):
            raise ValueError("Proposal source IDs must be unique")
        if any(item_id <= 0 for item_id in self.source_item_ids):
            raise ValueError("Proposal source IDs must be positive")
        references = int(self.task_id is not None) + int(self.existing_proposal_id is not None)
        if self.operation == "create_task":
            if references or not self.text or not self.text.strip() or self.fields:
                raise ValueError("create_task needs text, without an existing task reference or fields")
        elif references != 1:
            raise ValueError("Existing-task proposals need exactly one task reference")
        if self.operation == "update_task":
            if set(self.fields) != {"detail"} or not self.fields["detail"].strip():
                raise ValueError("update_task may only add a sourced detail")
            if len(self.fields["detail"]) > 2000:
                raise ValueError("Sourced details are limited to 2000 characters")
            if self.text is not None or self.timing_text is not None:
                raise ValueError("Sourced detail proposals cannot rewrite task text or timing")
        elif self.fields:
            raise ValueError("Only update_task accepts fields")
        if self.operation.startswith("flag_") and (self.text is not None or self.timing_text is not None):
            raise ValueError("Review proposals cannot rewrite task text or timing")
        return self


class LookupRequest(StrictDeltaModel):
    query: str = Field(default="", max_length=200)
    task_ids: list[int] = Field(default_factory=list, max_length=3)
    item_ids: list[int] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def targeted_lookup(self):
        if not self.query.strip() and not self.task_ids and not self.item_ids:
            raise ValueError("A lookup needs a query or stable IDs")
        if any(value <= 0 for value in self.task_ids + self.item_ids):
            raise ValueError("Lookup IDs must be positive")
        return self


class DeltaPlan(StrictDeltaModel):
    organizations: list[Organization] = Field(max_length=8)
    changes: list[ProposedChange] = Field(default_factory=list, max_length=100)
    lookup_requests: list[LookupRequest] = Field(default_factory=list, max_length=2)


def validate_plan(plan: dict | DeltaPlan, targets: list[dict], context: dict) -> dict:
    """Check all references against this batch, never against an entire inbox.

    Database reconciliation separately checks current ownership and source input
    fingerprints inside the transaction; model-visible IDs alone are not authority.
    """
    result = DeltaPlan.model_validate(plan)
    target_ids = {target["id"] for target in targets}
    organized_ids = [organization.item_id for organization in result.organizations]
    if len(organized_ids) != len(set(organized_ids)) or set(organized_ids) != target_ids:
        raise ValueError("Organizations must contain each analysis target exactly once")
    task_ids = {task["id"] for task in context.get("tasks", [])}
    seen = set()
    created = set()
    for change in result.changes:
        if change.proposal_id in seen:
            raise ValueError("Proposal IDs must be unique")
        if not set(change.source_item_ids) <= target_ids:
            raise ValueError("Proposals may only attach analysis targets")
        if change.task_id is not None and change.task_id not in task_ids:
            raise ValueError("Task reference is outside supporting context")
        if change.existing_proposal_id is not None and change.existing_proposal_id not in created:
            raise ValueError("Local task references must refer backward to a create_task proposal")
        seen.add(change.proposal_id)
        if change.operation == "create_task":
            created.add(change.proposal_id)
    return result.model_dump()
