from __future__ import annotations

import datetime as dt
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


MeetingStateStatus = Literal["ready", "lagging", "stale", "rebuilding"]
MarkOrigin = Literal["manual", "automatic"]
MarkKind = Literal["highlight", "decision", "action", "conflict"]
MarkStatus = Literal["candidate", "accepted", "dismissed"]
ActionReadiness = Literal[
    "detected",
    "recordable",
    "executable",
    "fully_specified",
]
CandidateContentStatus = Literal[
    "active",
    "dismissed",
    "superseded",
    "cancelled",
]
CandidateExecutionStatus = Literal[
    "not_requested",
    "handed_off",
    "executing",
    "executed",
    "failed",
]
CandidateChangeKind = Literal[
    "create",
    "revise",
    "merge",
    "split",
    "correction",
    "cancel",
]
GroundedValueOrigin = Literal[
    "meeting_explicit",
    "meeting_inferred",
    "user_supplied",
    "external_resolved",
    "server_default",
]
GroundedValueResolution = Literal[
    "known",
    "missing",
    "ambiguous",
    "conflicting",
]
TaskPriority = Literal["low", "normal", "high", "urgent"]
IdentityBindingStatus = Literal["confirmed", "revoked"]
RequiredCandidateField = Literal["assignee", "due_at", "priority"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptionEvidenceMessage(FrozenModel):
    message_kind: Literal["caption"] = "caption"
    message_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=36)
    actor_id: None = None
    speaker_label: str | None = Field(default=None, max_length=255)
    track_id: str = Field(min_length=1, max_length=255)
    segment_id: str = Field(min_length=1, max_length=255)
    segment_revision: int = Field(ge=1)
    language: str = Field(min_length=1, max_length=32)
    raw_text: str = Field(min_length=1)
    display_text: str = Field(min_length=1)
    audio_start_ms: int | None = Field(default=None, ge=0)
    audio_end_ms: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    received_at_ms: int = Field(ge=0)
    finalized_at: dt.datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_audio_bounds(self) -> CaptionEvidenceMessage:
        if (
            self.audio_start_ms is not None
            and self.audio_end_ms is not None
            and self.audio_end_ms < self.audio_start_ms
        ):
            raise ValueError("audio end must not precede audio start")
        return self


class UserInputEvidenceMessage(FrozenModel):
    message_kind: Literal["user_input"] = "user_input"
    message_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=36)
    actor_id: str = Field(min_length=1, max_length=255)
    raw_text: str = Field(min_length=1)
    display_text: str = Field(min_length=1)
    created_at: dt.datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


EvidenceMessageSnapshot = Annotated[
    CaptionEvidenceMessage | UserInputEvidenceMessage,
    Field(discriminator="message_kind"),
]


T = TypeVar("T")


class GroundedValue(FrozenModel, Generic[T]):
    value: T | None = None
    origin: GroundedValueOrigin
    resolution: GroundedValueResolution
    evidence_message_ids: tuple[str, ...] = Field(default_factory=tuple)
    confidence: float | None = Field(default=None, ge=0, le=1)
    explanation: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_grounding(self) -> GroundedValue[T]:
        if self.resolution == "known" and self.value is None:
            raise ValueError("known grounded values require a value")
        if self.origin != "server_default" and not self.evidence_message_ids:
            raise ValueError("non-default grounded values require evidence")
        if len(set(self.evidence_message_ids)) != len(self.evidence_message_ids):
            raise ValueError("evidence message IDs must be unique")
        return self


class AssigneeValue(FrozenModel):
    spoken_text: str = Field(min_length=1, max_length=255)
    linear_user_id: str | None = Field(default=None, max_length=255)
    is_placeholder: bool = False

    @model_validator(mode="after")
    def validate_placeholder(self) -> AssigneeValue:
        if self.is_placeholder and self.linear_user_id is not None:
            raise ValueError("placeholder assignees cannot have a Linear user ID")
        return self


class ActionCandidateContent(FrozenModel):
    title: GroundedValue[str]
    deliverable: GroundedValue[str]
    assignee: GroundedValue[AssigneeValue]
    due_at: GroundedValue[dt.datetime]
    priority: GroundedValue[TaskPriority]
    evidence_messages: tuple[EvidenceMessageSnapshot, ...] = Field(
        default_factory=tuple
    )
    blocking_conflict_message_ids: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_evidence_references(self) -> ActionCandidateContent:
        message_ids = [message.message_id for message in self.evidence_messages]
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("evidence message IDs must be unique")
        available = set(message_ids)
        referenced = set(self.blocking_conflict_message_ids)
        for grounded in (
            self.title,
            self.deliverable,
            self.assignee,
            self.due_at,
            self.priority,
        ):
            referenced.update(grounded.evidence_message_ids)
        missing = referenced - available
        if missing:
            raise ValueError(
                "candidate fields reference missing evidence messages: "
                + ", ".join(sorted(missing))
            )
        return self


class ActionCandidate(FrozenModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    lineage_root_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    current_revision: int = Field(ge=1)
    readiness: ActionReadiness = "detected"
    content_status: CandidateContentStatus = "active"
    execution_status: CandidateExecutionStatus = "not_requested"
    content: ActionCandidateContent
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    evidence_revisions_current: bool = True
    derived_from_candidate_id: str | None = Field(default=None, max_length=36)
    derived_from_revision: int | None = Field(default=None, ge=1)
    superseded_by_candidate_id: str | None = Field(default=None, max_length=36)


class ActionExecutionRequest(FrozenModel):
    explicitly_requested: bool = False
    required_fields: tuple[RequiredCandidateField, ...] = Field(
        default_factory=tuple
    )
    allow_unresolved_assignee_placeholder: bool = True


class ActionGrantContext(FrozenModel):
    active: bool = False
    session_id: str = Field(min_length=1, max_length=36)
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    candidate_ids: frozenset[str] = Field(default_factory=frozenset)
    remaining_side_effects: int = Field(default=0, ge=0)

    def allows_task_create(self, candidate: ActionCandidate) -> bool:
        return (
            self.active
            and self.session_id == candidate.session_id
            and "task.create" in self.capabilities
            and candidate.candidate_id in self.candidate_ids
            and self.remaining_side_effects > 0
        )


class MeetingStateItem(FrozenModel):
    item_id: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    source_segment_revisions: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_revisions(self) -> MeetingStateItem:
        if self.source_segment_revisions and set(
            self.source_segment_revisions
        ) != set(self.source_segment_ids):
            raise ValueError("source Segment revisions must match source IDs")
        if any(value < 1 for value in self.source_segment_revisions.values()):
            raise ValueError("source Segment revisions must be positive")
        return self


class Topic(MeetingStateItem):
    pass


class Entity(MeetingStateItem):
    entity_type: str = Field(min_length=1, max_length=64)


class Decision(MeetingStateItem):
    pass


class Highlight(MeetingStateItem):
    pass


class Conflict(MeetingStateItem):
    pass


class UserConcern(MeetingStateItem):
    pass


class MeetingState(FrozenModel):
    session_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=0)
    topics: tuple[Topic, ...] = Field(default_factory=tuple)
    entities: tuple[Entity, ...] = Field(default_factory=tuple)
    decisions: tuple[Decision, ...] = Field(default_factory=tuple)
    action_candidates: tuple[ActionCandidate, ...] = Field(default_factory=tuple)
    highlights: tuple[Highlight, ...] = Field(default_factory=tuple)
    conflicts: tuple[Conflict, ...] = Field(default_factory=tuple)
    user_concerns: tuple[UserConcern, ...] = Field(default_factory=tuple)


def _is_known(grounded: GroundedValue[object]) -> bool:
    return grounded.resolution == "known" and grounded.value is not None


def evaluate_action_readiness(
    candidate: ActionCandidate,
    execution_request: ActionExecutionRequest,
    grant: ActionGrantContext,
) -> ActionReadiness:
    """Derive candidate readiness without invoking a model or external tool."""

    if candidate.content_status != "active":
        return candidate.readiness

    content = candidate.content
    if not (
        _is_known(content.title)
        and _is_known(content.deliverable)
        and content.title.evidence_message_ids
        and content.deliverable.evidence_message_ids
    ):
        return "detected"

    readiness: ActionReadiness = "recordable"
    if (
        not execution_request.explicitly_requested
        or not candidate.evidence_revisions_current
        or content.blocking_conflict_message_ids
        or not grant.allows_task_create(candidate)
    ):
        return readiness

    for required_field in execution_request.required_fields:
        grounded = getattr(content, required_field)
        if _is_known(grounded):
            continue
        if (
            required_field == "assignee"
            and execution_request.allow_unresolved_assignee_placeholder
            and isinstance(grounded.value, AssigneeValue)
            and grounded.value.is_placeholder
        ):
            continue
        return readiness

    if all(
        _is_known(grounded)
        for grounded in (content.assignee, content.due_at, content.priority)
    ):
        return "fully_specified"
    return "executable"
