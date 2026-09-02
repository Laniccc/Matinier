from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskPriority = Literal["low", "normal", "high", "urgent"]
TaskStatus = Literal["active", "completed", "cancelled"]
IdentityResolution = Literal["known", "missing", "ambiguous"]
IdentitySource = Literal[
    "explicit_id",
    "email",
    "confirmed_binding",
    "exact_display_name",
    "placeholder",
]
TaskDisposition = Literal[
    "same_action",
    "duplicate",
    "related",
    "distinct",
    "ambiguous",
]


class FrozenTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskSystemConnection(FrozenTaskModel):
    provider: str = Field(min_length=1, max_length=64)
    team_id: str = Field(min_length=1, max_length=255)
    workspace_id: str | None = Field(default=None, max_length=255)
    default_project_id: str | None = Field(default=None, max_length=255)
    default_label_ids: tuple[str, ...] = Field(default_factory=tuple)


class TaskDraft(FrozenTaskModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    assignee_ref: str | None = Field(default=None, max_length=255)
    due_at: dt.datetime | None = None
    priority: TaskPriority | None = None
    source_evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    related_task_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=50)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("task title must not be blank")
        return normalized

    @field_validator("source_evidence_ids", "related_task_refs")
    @classmethod
    def unique_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("task references must be unique")
        return value


class ExternalTask(FrozenTaskModel):
    external_id: str = Field(min_length=1, max_length=255)
    identifier: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=2_000)
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    assignee_ref: str | None = Field(default=None, max_length=255)
    due_at: dt.datetime | None = None
    priority: TaskPriority | None = None
    status: TaskStatus = "active"
    team_id: str = Field(min_length=1, max_length=255)
    project_id: str | None = Field(default=None, max_length=255)
    action_key: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    created_at: dt.datetime
    updated_at: dt.datetime


class TaskSearchQuery(FrozenTaskModel):
    title: str | None = Field(default=None, max_length=500)
    assignee_ref: str | None = Field(default=None, max_length=255)
    due_at: dt.datetime | None = None
    action_key: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    limit: int = Field(default=20, ge=1, le=20)


class TaskSearchResult(FrozenTaskModel):
    tasks: tuple[ExternalTask, ...] = Field(default_factory=tuple, max_length=20)


class TaskCreateResult(FrozenTaskModel):
    task: ExternalTask
    created: bool


class TaskReconciliationResult(FrozenTaskModel):
    status: Literal["none", "single", "multiple"]
    matches: tuple[ExternalTask, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def match_count_agrees(self) -> TaskReconciliationResult:
        expected = "none" if not self.matches else (
            "single" if len(self.matches) == 1 else "multiple"
        )
        if self.status != expected:
            raise ValueError("reconciliation status does not match its tasks")
        return self


class ExternalMember(FrozenTaskModel):
    external_user_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    team_ids: tuple[str, ...] = Field(default_factory=tuple)
    active: bool = True


class PersonMention(FrozenTaskModel):
    spoken_text: str = Field(min_length=1, max_length=255)
    actor_id: str = Field(min_length=1, max_length=255)
    explicit_external_user_id: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    authenticated_speaker_actor_id: str | None = Field(
        default=None,
        max_length=255,
    )


class ResolvedIdentity(FrozenTaskModel):
    spoken_text: str = Field(min_length=1, max_length=255)
    external_user_id: str | None = Field(default=None, max_length=255)
    resolution: IdentityResolution
    source: IdentitySource
    is_placeholder: bool

    @model_validator(mode="after")
    def validate_resolution(self) -> ResolvedIdentity:
        if self.resolution == "known":
            if self.external_user_id is None or self.is_placeholder:
                raise ValueError("known identities require a real external user")
        elif self.external_user_id is not None or not self.is_placeholder:
            raise ValueError("unresolved identities must remain placeholders")
        return self


class IdentityBinding(FrozenTaskModel):
    binding_id: str = Field(min_length=1, max_length=36)
    actor_id: str = Field(min_length=1, max_length=255)
    team_id: str = Field(min_length=1, max_length=255)
    normalized_mention: str = Field(min_length=1, max_length=255)
    external_user_id: str = Field(min_length=1, max_length=255)
    status: Literal["confirmed", "revoked"]
    confirmed_by_actor_id: str = Field(min_length=1, max_length=255)
    last_verified_at: dt.datetime | None = None


class TaskDecisionContext(FrozenTaskModel):
    evidence_confirms_single_deliverable: bool
    conflict_agent_agrees: bool
    research_agent_agrees: bool


class TaskServiceOutcome(FrozenTaskModel):
    disposition: TaskDisposition
    action_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    task: ExternalTask | None = None
    created: bool = False
    partial: bool = False
    unresolved_identity: ResolvedIdentity | None = None
    related_task_refs: tuple[str, ...] = Field(default_factory=tuple)


__all__ = [
    "ExternalMember",
    "ExternalTask",
    "IdentityBinding",
    "PersonMention",
    "ResolvedIdentity",
    "TaskCreateResult",
    "TaskDecisionContext",
    "TaskDisposition",
    "TaskDraft",
    "TaskPriority",
    "TaskReconciliationResult",
    "TaskSearchQuery",
    "TaskSearchResult",
    "TaskServiceOutcome",
    "TaskStatus",
    "TaskSystemConnection",
]
