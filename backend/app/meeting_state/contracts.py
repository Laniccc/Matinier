from __future__ import annotations

import datetime as dt
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.meeting_state.models import (
    GroundedValueResolution,
    TaskPriority,
)


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectionSegment(FrozenContract):
    session_id: str = Field(min_length=1, max_length=36)
    segment_id: str = Field(min_length=1, max_length=255)
    revision: int = Field(ge=1)
    track_id: str = Field(min_length=1, max_length=255)
    language: str = Field(min_length=1, max_length=32)
    raw_text: str = Field(min_length=1)
    display_text: str = Field(min_length=1)
    audio_start_ms: int | None = Field(default=None, ge=0)
    audio_end_ms: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    received_at_ms: int = Field(ge=0)
    finalized_at: dt.datetime
    updated_at: dt.datetime

    @model_validator(mode="after")
    def validate_audio_bounds(self) -> ProjectionSegment:
        if (
            self.audio_start_ms is not None
            and self.audio_end_ms is not None
            and self.audio_end_ms < self.audio_start_ms
        ):
            raise ValueError("audio end must not precede audio start")
        return self


class StateItemProposal(FrozenContract):
    text: str = Field(min_length=1)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("state item text must not be blank")
        return normalized

    @field_validator("source_segment_ids")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("source Segment IDs must be unique")
        return value


class EntityProposal(StateItemProposal):
    entity_type: str = Field(min_length=1, max_length=64)


T = TypeVar("T")


class GroundedValueProposal(FrozenContract, Generic[T]):
    value: T | None = None
    resolution: GroundedValueResolution
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    explanation: str | None = Field(default=None, max_length=1000)

    @field_validator("source_segment_ids")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("grounded field evidence must be unique")
        return value

    @model_validator(mode="after")
    def validate_resolution(self) -> GroundedValueProposal[T]:
        if self.resolution == "known" and self.value is None:
            raise ValueError("known grounded fields require a value")
        return self


class CandidateContentProposal(FrozenContract):
    title: GroundedValueProposal[str]
    deliverable: GroundedValueProposal[str]
    assignee: GroundedValueProposal[str]
    due_at: GroundedValueProposal[dt.datetime]
    priority: GroundedValueProposal[TaskPriority]
    blocking_conflict_segment_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("blocking_conflict_segment_ids")
    @classmethod
    def unique_conflicts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("blocking conflict evidence must be unique")
        return value

    @property
    def source_segment_ids(self) -> tuple[str, ...]:
        values: list[str] = []
        for grounded in (
            self.title,
            self.deliverable,
            self.assignee,
            self.due_at,
            self.priority,
        ):
            values.extend(grounded.source_segment_ids)
        values.extend(self.blocking_conflict_segment_ids)
        return tuple(dict.fromkeys(values))


class CreateCandidateOperation(FrozenContract):
    kind: Literal["create"] = "create"
    content: CandidateContentProposal
    change_summary: str | None = Field(default=None, max_length=1000)


class ReviseCandidateOperation(FrozenContract):
    kind: Literal["revise"] = "revise"
    candidate_id: str = Field(min_length=1, max_length=36)
    expected_revision: int = Field(ge=1)
    content: CandidateContentProposal
    change_summary: str | None = Field(default=None, max_length=1000)


class MergeCandidatesOperation(FrozenContract):
    kind: Literal["merge"] = "merge"
    candidate_ids: tuple[str, str]
    expected_revisions: dict[str, int]
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    change_summary: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_candidates(self) -> MergeCandidatesOperation:
        if self.candidate_ids[0] == self.candidate_ids[1]:
            raise ValueError("merge requires two distinct candidates")
        if set(self.expected_revisions) != set(self.candidate_ids):
            raise ValueError("merge revisions must match candidate IDs")
        if any(revision < 1 for revision in self.expected_revisions.values()):
            raise ValueError("merge revisions must be positive")
        if len(set(self.source_segment_ids)) != len(self.source_segment_ids):
            raise ValueError("merge evidence must be unique")
        return self


class SplitCandidateOperation(FrozenContract):
    kind: Literal["split"] = "split"
    candidate_id: str = Field(min_length=1, max_length=36)
    expected_revision: int = Field(ge=1)
    parts: tuple[CandidateContentProposal, ...] = Field(min_length=2)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    change_summary: str | None = Field(default=None, max_length=1000)


class CancelCandidateOperation(FrozenContract):
    kind: Literal["cancel"] = "cancel"
    candidate_id: str = Field(min_length=1, max_length=36)
    expected_revision: int = Field(ge=1)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


CandidateOperation = Annotated[
    CreateCandidateOperation
    | ReviseCandidateOperation
    | MergeCandidatesOperation
    | SplitCandidateOperation
    | CancelCandidateOperation,
    Field(discriminator="kind"),
]


class MeetingStateDelta(FrozenContract):
    """Validated extraction result bound to one server-selected Segment batch."""

    session_id: str = Field(min_length=1, max_length=36)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    topics: tuple[StateItemProposal, ...] = Field(default_factory=tuple)
    entities: tuple[EntityProposal, ...] = Field(default_factory=tuple)
    decisions: tuple[StateItemProposal, ...] = Field(default_factory=tuple)
    highlights: tuple[StateItemProposal, ...] = Field(default_factory=tuple)
    conflicts: tuple[StateItemProposal, ...] = Field(default_factory=tuple)
    action_operations: tuple[CandidateOperation, ...] = Field(
        default_factory=tuple
    )
    warnings: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("source_segment_ids")
    @classmethod
    def unique_batch_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("projection batch Segment IDs must be unique")
        return value

    def cited_segment_ids(self) -> frozenset[str]:
        cited: set[str] = set()
        for collection in (
            self.topics,
            self.entities,
            self.decisions,
            self.highlights,
            self.conflicts,
        ):
            for item in collection:
                cited.update(item.source_segment_ids)
        for operation in self.action_operations:
            if isinstance(operation, (CreateCandidateOperation, ReviseCandidateOperation)):
                cited.update(operation.content.source_segment_ids)
            elif isinstance(operation, MergeCandidatesOperation):
                cited.update(operation.source_segment_ids)
            elif isinstance(operation, SplitCandidateOperation):
                cited.update(operation.source_segment_ids)
                for part in operation.parts:
                    cited.update(part.source_segment_ids)
            else:
                cited.update(operation.source_segment_ids)
        return frozenset(cited)
