from __future__ import annotations

import datetime as dt
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant.models import (
    ContextSnapshot,
    HandoffEnvelope,
    ObservationSource,
    StepKind,
)
from app.assistant.parser import EvidenceClaim
from app.assistant.repository import AssistantRepository
from app.persistence.database import Database
from app.persistence.models import AssistantContextSnapshotRecord


class FrozenHandoffModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HandoffCompletedStep(FrozenHandoffModel):
    kind: StepKind
    input_payload: dict[str, Any]
    output_payload: dict[str, Any] | None = None
    decision_summary: str | None = Field(default=None, max_length=1000)


class HandoffObservation(FrozenHandoffModel):
    source: ObservationSource
    observation: dict[str, Any]
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    source_ref: str | None = Field(default=None, max_length=255)


class HandoffGrantRequest(FrozenHandoffModel):
    actor_id: str = Field(min_length=1, max_length=255)
    capabilities: tuple[str, ...]
    resource_scope: dict[str, Any]
    candidate_ids: tuple[str, ...] = Field(default_factory=tuple)
    max_side_effects: int = Field(ge=1)
    expires_at: dt.datetime

    @model_validator(mode="after")
    def unique_scope_values(self) -> HandoffGrantRequest:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Grant capabilities must be unique")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("Grant candidate IDs must be unique")
        return self


class HandoffCommit(FrozenHandoffModel):
    handoff_id: str = Field(min_length=1, max_length=36)
    source_execution_id: str = Field(min_length=1, max_length=36)
    target_execution_id: str = Field(min_length=1, max_length=36)
    root_execution_id: str = Field(min_length=1, max_length=36)
    grant_id: str | None = Field(default=None, max_length=36)
    snapshot_id: str = Field(min_length=1, max_length=36)


class HandoffService:
    """Atomically transfers a Fast Turn into one queued Action Run."""

    def __init__(
        self,
        database: Database,
        *,
        enqueue: Callable[[str], Awaitable[None] | None] | None = None,
        execution_guard=None,
    ) -> None:
        self._database = database
        self._enqueue = enqueue
        self._execution_guard = execution_guard

    async def handoff(
        self,
        *,
        source_execution_id: str,
        expected_state_version: int,
        snapshot: ContextSnapshot,
        handoff_goal: str,
        reason: EvidenceClaim,
        completed_steps: Sequence[HandoffCompletedStep] = (),
        observations: Sequence[HandoffObservation] = (),
        unresolved_conflicts: Sequence[Mapping[str, Any]] = (),
        candidate_plan: Mapping[str, Any] | None = None,
        remaining_budget: Mapping[str, Any] | None = None,
        grant_id: str | None = None,
        grant_request: HandoffGrantRequest | None = None,
        idempotency_scope: str | None = None,
    ) -> HandoffCommit:
        normalized_goal = " ".join(handoff_goal.split())
        if not normalized_goal:
            raise ValueError("handoff goal is required")
        if grant_id is not None and grant_request is not None:
            raise ValueError("handoff may reference or create one Grant, not both")
        if not set(reason.evidence_refs).issubset(snapshot.evidence_refs):
            raise ValueError("handoff reason references unavailable evidence")

        with self._database.session() as db_session:
            if self._execution_guard is not None:
                self._execution_guard.fence(db_session, source_execution_id, allow_unowned_read=True)
            repository = AssistantRepository(db_session)
            existing = repository.get_handoff_by_source(source_execution_id)
            if existing is not None:
                envelope = HandoffEnvelope.model_validate(existing.envelope_json)
                target = repository.get_execution_required(
                    existing.target_execution_id
                )
                commit = HandoffCommit(
                    handoff_id=existing.id,
                    source_execution_id=source_execution_id,
                    target_execution_id=target.id,
                    root_execution_id=target.root_execution_id,
                    grant_id=existing.grant_id,
                    snapshot_id=existing.snapshot_id,
                )
                if envelope.context_snapshot_id != existing.snapshot_id:
                    raise RuntimeError("persisted Handoff snapshot is inconsistent")
            else:
                source = repository.get_execution_required(source_execution_id)
                if source.profile != "fast_turn":
                    raise ValueError("only a Fast Turn can create a Handoff")
                if source.session_id != snapshot.session_id:
                    raise ValueError("handoff snapshot belongs to another Session")
                if source.state_version != expected_state_version:
                    raise ValueError("Fast Turn changed before Handoff")
                if source.snapshot_id not in {None, snapshot.snapshot_id}:
                    raise ValueError("Fast Turn is bound to another Context Snapshot")
                self._freeze_snapshot(repository, snapshot)
                resolved_grant_id = self._resolve_grant(
                    repository,
                    session_id=source.session_id,
                    handoff_goal=normalized_goal,
                    grant_id=grant_id,
                    grant_request=grant_request,
                )
                target_id = str(uuid.uuid4())
                target = repository.create_execution(
                    execution_id=target_id,
                    session_id=source.session_id,
                    profile="action_run",
                    goal=normalized_goal,
                    parent_execution_id=source.id,
                    root_execution_id=source.root_execution_id,
                    snapshot_id=snapshot.snapshot_id,
                    grant_id=resolved_grant_id,
                    budget=dict(remaining_budget or {}),
                )
                for step in completed_steps:
                    repository.append_step(
                        execution_id=source.id,
                        kind=step.kind,
                        input_payload=step.input_payload,
                        output_payload=step.output_payload,
                        decision_summary=step.decision_summary,
                    )
                for observation in observations:
                    repository.append_observation(
                        execution_id=source.id,
                        source=observation.source,
                        source_ref=observation.source_ref,
                        observation=observation.observation,
                        evidence_refs=observation.evidence_refs,
                    )
                envelope = HandoffEnvelope(
                    handoff_id=str(uuid.uuid4()),
                    parent_execution_id=source.id,
                    target_execution_id=target.id,
                    session_id=source.session_id,
                    goal=normalized_goal,
                    grant_id=resolved_grant_id,
                    context_snapshot_id=snapshot.snapshot_id,
                    meeting_state_version=snapshot.meeting_state_version,
                    evidence_refs=snapshot.evidence_refs,
                    completed_steps=tuple(
                        step.model_dump(mode="json") for step in completed_steps
                    ),
                    observations=tuple(
                        observation.model_dump(mode="json")
                        for observation in observations
                    ),
                    unresolved_conflicts=tuple(
                        dict(value) for value in unresolved_conflicts
                    ),
                    candidate_plan=dict(candidate_plan or {}),
                    remaining_budget=dict(remaining_budget or {}),
                    idempotency_scope=(
                        idempotency_scope
                        or f"handoff:{source.root_execution_id}:{source.id}"
                    ),
                )
                repository.create_handoff(envelope)
                source = repository.transition_execution(
                    source.id,
                    expected_version=expected_state_version,
                    target_status="handed_off",
                    event_type="handoff.committed",
                    summary="Fast Turn handed off to a durable Action Run",
                    payload={
                        "target_execution_id": target.id,
                        "handoff_id": envelope.handoff_id,
                    },
                    snapshot_id=snapshot.snapshot_id,
                    result={
                        "handoff_execution_id": target.id,
                        "handoff_id": envelope.handoff_id,
                    },
                )
                repository.append_event(
                    execution_id=target.id,
                    event_type="handoff.received",
                    phase="queued",
                    summary="Action Run received a Fast Turn handoff",
                    payload={
                        "source_execution_id": source.id,
                        "handoff_id": envelope.handoff_id,
                    },
                )
                commit = HandoffCommit(
                    handoff_id=envelope.handoff_id,
                    source_execution_id=source.id,
                    target_execution_id=target.id,
                    root_execution_id=target.root_execution_id,
                    grant_id=resolved_grant_id,
                    snapshot_id=snapshot.snapshot_id,
                )
                db_session.commit()

        await self._enqueue_after_commit(commit.target_execution_id)
        return commit

    @staticmethod
    def _freeze_snapshot(
        repository: AssistantRepository,
        snapshot: ContextSnapshot,
    ) -> AssistantContextSnapshotRecord:
        existing = repository.get_context_snapshot(snapshot.snapshot_id)
        if existing is None:
            return repository.create_context_snapshot(
                snapshot_id=snapshot.snapshot_id,
                session_id=snapshot.session_id,
                meeting_state_version=snapshot.meeting_state_version,
                state_slice=snapshot.state_slice,
                source_frontier=snapshot.source_frontier,
                evidence_refs=snapshot.evidence_refs,
                evidence_messages=snapshot.evidence_messages,
                relevant_context_hash=snapshot.relevant_context_hash,
                created_at=snapshot.created_at,
            )
        if (
            existing.session_id != snapshot.session_id
            or existing.meeting_state_version != snapshot.meeting_state_version
            or existing.relevant_context_hash != snapshot.relevant_context_hash
        ):
            raise ValueError("persisted Context Snapshot does not match Handoff")
        return existing

    @staticmethod
    def _resolve_grant(
        repository: AssistantRepository,
        *,
        session_id: str,
        handoff_goal: str,
        grant_id: str | None,
        grant_request: HandoffGrantRequest | None,
    ) -> str | None:
        if grant_id is not None:
            record = repository.get_grant(grant_id)
            if record is None:
                raise LookupError(f"Action Grant not found: {grant_id}")
            if record.session_id != session_id:
                raise ValueError("Action Grant belongs to another Session")
            if " ".join(record.goal.split()) != handoff_goal:
                raise ValueError("Action Grant does not cover the Handoff goal")
            return record.id
        if grant_request is None:
            return None
        record = repository.create_grant(
            session_id=session_id,
            actor_id=grant_request.actor_id,
            goal=handoff_goal,
            capabilities=grant_request.capabilities,
            resource_scope=grant_request.resource_scope,
            candidate_ids=grant_request.candidate_ids,
            max_side_effects=grant_request.max_side_effects,
            expires_at=grant_request.expires_at,
        )
        return record.id

    async def _enqueue_after_commit(self, execution_id: str) -> None:
        if self._enqueue is None:
            return
        result = self._enqueue(execution_id)
        if inspect.isawaitable(result):
            await result


__all__ = [
    "HandoffCommit",
    "HandoffCompletedStep",
    "HandoffGrantRequest",
    "HandoffObservation",
    "HandoffService",
]
