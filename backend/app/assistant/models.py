from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant.state_machine import (
    ActionRunStatus,
    ExecutionProfile,
    ExecutionStatus,
    ExternalActionClaimStatus,
    FastTurnStatus,
    SubagentStatus,
    ToolCallStatus,
    validate_execution_status,
)
from app.meeting_state.models import EvidenceMessageSnapshot


GrantStatus = Literal["active", "consumed", "revoked", "expired"]
StepKind = Literal["plan", "tool", "observe", "reconcile", "respond"]
ToolEffect = Literal["read", "local_write", "external_write"]
ObservationSource = Literal["tool", "model", "system", "user"]
SubagentRole = Literal["evidence", "conflict", "linear_research"]
ClientOperationKind = Literal["input", "cancel"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextSnapshot(FrozenModel):
    snapshot_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    meeting_state_version: int = Field(ge=0)
    state_slice: dict[str, Any]
    source_frontier: dict[str, Any]
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    evidence_messages: tuple[EvidenceMessageSnapshot, ...]
    relevant_context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: dt.datetime

    @model_validator(mode="after")
    def validate_evidence_refs(self) -> ContextSnapshot:
        message_ids = {message.message_id for message in self.evidence_messages}
        missing = set(self.evidence_refs) - message_ids
        if missing:
            raise ValueError(
                "snapshot references missing evidence messages: "
                + ", ".join(sorted(missing))
            )
        return self


class ActionGrant(FrozenModel):
    grant_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    actor_id: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1)
    capabilities: frozenset[str]
    resource_scope: dict[str, Any]
    candidate_ids: frozenset[str]
    linear_team_id: str | None = Field(default=None, max_length=255)
    max_side_effects: int = Field(ge=0)
    used_side_effects: int = Field(ge=0)
    unresolved_identity_policy: Literal["placeholder"] = "placeholder"
    expires_at: dt.datetime
    status: GrantStatus
    created_at: dt.datetime

    @model_validator(mode="after")
    def validate_usage(self) -> ActionGrant:
        if self.used_side_effects > self.max_side_effects:
            raise ValueError("used side effects exceed the Grant maximum")
        return self


class AssistantExecution(FrozenModel):
    execution_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    profile: ExecutionProfile
    root_execution_id: str = Field(min_length=1, max_length=36)
    parent_execution_id: str | None = Field(default=None, max_length=36)
    snapshot_id: str | None = Field(default=None, max_length=36)
    grant_id: str | None = Field(default=None, max_length=36)
    client_request_id: str | None = Field(default=None, max_length=255)
    goal: str = Field(min_length=1)
    status: FastTurnStatus | ActionRunStatus
    state_version: int = Field(ge=1)
    step_count: int = Field(ge=0)
    budget: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)
    created_at: dt.datetime
    updated_at: dt.datetime
    completed_at: dt.datetime | None

    @model_validator(mode="after")
    def validate_profile_status(self) -> AssistantExecution:
        validate_execution_status(self.profile, self.status)
        return self


class AssistantEvent(FrozenModel):
    event_id: int = Field(ge=1)
    session_id: str = Field(min_length=1, max_length=36)
    execution_id: str = Field(min_length=1, max_length=36)
    root_execution_id: str = Field(min_length=1, max_length=36)
    state_version: int = Field(ge=1)
    schema_version: Literal[1] = 1
    event_type: str = Field(min_length=1, max_length=64)
    phase: str | None = Field(default=None, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any]
    created_at: dt.datetime


class HandoffEnvelope(FrozenModel):
    handoff_id: str = Field(min_length=1, max_length=36)
    parent_execution_id: str = Field(min_length=1, max_length=36)
    target_execution_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    goal: str = Field(min_length=1)
    grant_id: str | None = Field(default=None, max_length=36)
    context_snapshot_id: str = Field(min_length=1, max_length=36)
    meeting_state_version: int = Field(ge=0)
    evidence_refs: tuple[str, ...]
    completed_steps: tuple[dict[str, Any], ...]
    observations: tuple[dict[str, Any], ...]
    unresolved_conflicts: tuple[dict[str, Any], ...]
    candidate_plan: dict[str, Any]
    remaining_budget: dict[str, Any]
    idempotency_scope: str = Field(min_length=1, max_length=255)


__all__ = [
    "ActionGrant",
    "ActionRunStatus",
    "AssistantEvent",
    "AssistantExecution",
    "ClientOperationKind",
    "ContextSnapshot",
    "ExecutionProfile",
    "ExecutionStatus",
    "ExternalActionClaimStatus",
    "FastTurnStatus",
    "GrantStatus",
    "HandoffEnvelope",
    "ObservationSource",
    "StepKind",
    "SubagentRole",
    "SubagentStatus",
    "ToolCallStatus",
    "ToolEffect",
]
