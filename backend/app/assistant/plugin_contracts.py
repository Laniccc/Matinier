"""Bounded meeting capability inputs. Actor and session come from the Host."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Version = Annotated[int, Field(ge=1, strict=True)]


class MeetingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class MeetingCommandInput(MeetingContract):
    request_id: Identifier
    intent_token: str = Field(min_length=32, max_length=256, repr=False)


class MeetingAskInput(MeetingCommandInput):
    message: str = Field(min_length=1, max_length=4000)
    mark_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=50)

    @field_validator("mark_ids")
    @classmethod
    def unique_marks(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("mark IDs must be unique")
        return values


class MeetingExecuteInput(MeetingAskInput):
    candidate_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=20)

    @field_validator("candidate_ids")
    @classmethod
    def unique_candidates(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("candidate IDs must be unique")
        return values


class MeetingEvidenceRef(MeetingContract):
    segment_id: Identifier
    revision: Version


class MeetingMarkInput(MeetingCommandInput):
    operation: Literal["create", "accept", "dismiss"]
    mark_id: Identifier | None = None
    expected_state_version: Annotated[int, Field(ge=0, strict=True)] | None = None
    kind: Literal["highlight", "decision", "action", "conflict"] = "highlight"
    title: str | None = Field(default=None, min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=2000)
    evidence: tuple[MeetingEvidenceRef, ...] = Field(default_factory=tuple, max_length=50)

    @model_validator(mode="after")
    def evidence_and_target(self):
        if len({e.segment_id for e in self.evidence}) != len(self.evidence):
            raise ValueError("evidence IDs must be unique")
        if self.operation == "create":
            if not self.title or not self.evidence or self.mark_id is not None:
                raise ValueError("creating a mark requires title and evidence, not a mark ID")
        elif self.mark_id is None or self.expected_state_version is None:
            raise ValueError("updating a mark requires its ID and state version")
        if self.operation == "accept" and not self.evidence:
            raise ValueError("accepting a mark requires its evidence revisions")
        return self


class MeetingCancelInput(MeetingCommandInput):
    execution_id: Identifier
    expected_state_version: Version


class MeetingInputInput(MeetingCancelInput):
    input: str = Field(min_length=1, max_length=4000)


class MeetingStateQueryInput(MeetingContract):
    after: int = Field(default=0, ge=0, strict=True)
    offset: int = Field(default=0, ge=0, le=1_000_000, strict=True)
    limit: int = Field(default=50, ge=1, le=100, strict=True)
    known_version: int | None = Field(default=None, ge=0, strict=True)


class MeetingOperationQueryInput(MeetingStateQueryInput):
    operation_id: Identifier | None = None
    execution_id: Identifier | None = None

    @model_validator(mode="after")
    def one_target(self):
        if (self.operation_id is None) == (self.execution_id is None):
            raise ValueError("exactly one operation or execution ID is required")
        return self


class MeetingOperationAccepted(MeetingContract):
    operation_id: str = Field(min_length=1, max_length=36)
    status: Literal["accepted"] = "accepted"


class MeetingQueryOutput(MeetingContract):
    data: dict[str, object]

    @field_validator("data")
    @classmethod
    def bounded_data(cls, value):
        from app.media.contracts import validate_bounded_json
        validate_bounded_json(value, max_bytes=256 * 1024)
        return value
