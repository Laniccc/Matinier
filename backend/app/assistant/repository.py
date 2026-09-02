from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logging import log_assistant_lifecycle
from app.assistant.models import (
    ClientOperationKind,
    GrantStatus,
    HandoffEnvelope,
    ObservationSource,
    StepKind,
    SubagentRole,
    SubagentStatus,
    ToolCallStatus,
    ToolEffect,
)
from app.assistant.state_machine import (
    ExecutionProfile,
    ExternalActionClaimStatus,
    initial_execution_status,
    is_terminal_execution_status,
    transition_execution_status,
    transition_external_action_claim,
    transition_subagent_status,
    transition_tool_call_status,
)
from app.meeting_state.models import EvidenceMessageSnapshot
from app.persistence.models import (
    ActionGrantRecord,
    AssistantClientOperationRecord,
    AssistantContextSnapshotRecord,
    AssistantEventRecord,
    AssistantExecutionRecord,
    AssistantHandoffRecord,
    AssistantObservationRecord,
    AssistantStepRecord,
    AssistantSubagentRunRecord,
    AssistantToolCallRecord,
    ExternalActionClaimRecord,
    utc_now,
)


class AssistantStateConflictError(RuntimeError):
    pass


class AssistantIdempotencyConflictError(RuntimeError):
    pass


class ActionGrantUnavailableError(RuntimeError):
    pass


def _require_digest(value: str, field_name: str) -> None:
    if len(value) != 64:
        raise ValueError(f"{field_name} must be a 64-character digest")


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _elapsed_ms(started_at: dt.datetime, ended_at: dt.datetime) -> int:
    return max(
        0,
        int((_as_utc(ended_at) - _as_utc(started_at)).total_seconds() * 1_000),
    )


def _assistant_lifecycle_event(
    source_event: str,
    *,
    status: str,
) -> str | None:
    if source_event == "execution.created":
        return "assistant_turn_accepted"
    if source_event.endswith("context_frozen"):
        return "assistant_context_frozen"
    if source_event == "handoff.committed":
        return "assistant_handoff_committed"
    if status == "needs_input":
        return "assistant_needs_input"
    if status in {"completed", "partial", "failed", "cancelled"}:
        return "assistant_terminal_result"
    return None


class AssistantRepository:
    """Transaction-friendly persistence operations for both execution profiles."""

    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def export_trace(self, execution_id: str, *, media_session_id: str | None = None):
        from app.assistant.trace import export_agent_trace

        return export_agent_trace(
            self._db_session,
            execution_id,
            media_session_id=media_session_id,
        )

    def create_context_snapshot(
        self,
        *,
        session_id: str,
        meeting_state_version: int,
        state_slice: Mapping[str, object],
        source_frontier: Mapping[str, object],
        evidence_refs: Sequence[str],
        evidence_messages: Sequence[EvidenceMessageSnapshot],
        relevant_context_hash: str,
        snapshot_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> AssistantContextSnapshotRecord:
        _require_digest(relevant_context_hash, "relevant_context_hash")
        record = AssistantContextSnapshotRecord(
            id=snapshot_id or str(uuid.uuid4()),
            session_id=session_id,
            meeting_state_version=meeting_state_version,
            state_slice_json=dict(state_slice),
            source_frontier_json=dict(source_frontier),
            evidence_refs_json=list(dict.fromkeys(evidence_refs)),
            evidence_messages_json=[
                message.model_dump(mode="json") for message in evidence_messages
            ],
            relevant_context_hash=relevant_context_hash,
            created_at=created_at or utc_now(),
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def get_context_snapshot(
        self,
        snapshot_id: str,
    ) -> AssistantContextSnapshotRecord | None:
        return self._db_session.get(AssistantContextSnapshotRecord, snapshot_id)

    def create_execution(
        self,
        *,
        session_id: str,
        profile: ExecutionProfile,
        goal: str,
        client_request_id: str | None = None,
        parent_execution_id: str | None = None,
        root_execution_id: str | None = None,
        snapshot_id: str | None = None,
        grant_id: str | None = None,
        budget: Mapping[str, object] | None = None,
        execution_id: str | None = None,
        created_at: dt.datetime | None = None,
        use_savepoint: bool = True,
    ) -> AssistantExecutionRecord:
        if not goal.strip():
            raise ValueError("execution goal is required")
        if client_request_id is not None:
            existing = self._db_session.scalar(
                select(AssistantExecutionRecord).where(
                    AssistantExecutionRecord.session_id == session_id,
                    AssistantExecutionRecord.client_request_id
                    == client_request_id,
                )
            )
            if existing is not None:
                return existing

        identifier = execution_id or str(uuid.uuid4())
        if parent_execution_id is not None:
            parent = self.get_execution_required(parent_execution_id)
            if parent.session_id != session_id:
                raise ValueError("parent execution belongs to a different Session")
            resolved_root = root_execution_id or parent.root_execution_id
            if resolved_root != parent.root_execution_id:
                raise ValueError("child execution must inherit the parent root")
        else:
            resolved_root = root_execution_id or identifier
            if resolved_root != identifier:
                raise ValueError("a root execution must reference itself")

        timestamp = created_at or utc_now()
        status = initial_execution_status(profile)
        record = AssistantExecutionRecord(
            id=identifier,
            session_id=session_id,
            profile=profile,
            root_execution_id=resolved_root,
            parent_execution_id=parent_execution_id,
            snapshot_id=snapshot_id,
            grant_id=grant_id,
            client_request_id=client_request_id,
            goal=goal.strip(),
            status=status,
            state_version=1,
            step_count=0,
            budget_json=dict(budget or {}),
            result_json=None,
            error_code=None,
            error_message=None,
            created_at=timestamp,
            started_at=None,
            updated_at=timestamp,
            completed_at=None,
        )
        try:
            if use_savepoint:
                with self._db_session.begin_nested():
                    self._db_session.add(record)
                    self._db_session.flush()
            else:
                # Caller already serializes durable operation admission.
                self._db_session.add(record)
                self._db_session.flush()
        except IntegrityError:
            if client_request_id is None:
                raise
            existing = self.get_execution_by_client_request(
                session_id=session_id,
                client_request_id=client_request_id,
            )
            if existing is None:
                raise
            return existing
        self.append_event(
            execution_id=identifier,
            event_type="execution.created",
            phase=status,
            summary="Execution accepted",
            payload={},
            state_version=1,
            created_at=timestamp,
        )
        return record

    def get_execution(self, execution_id: str) -> AssistantExecutionRecord | None:
        return self._db_session.get(AssistantExecutionRecord, execution_id)

    def get_execution_by_client_request(
        self,
        *,
        session_id: str,
        client_request_id: str,
    ) -> AssistantExecutionRecord | None:
        return self._db_session.scalar(
            select(AssistantExecutionRecord).where(
                AssistantExecutionRecord.session_id == session_id,
                AssistantExecutionRecord.client_request_id
                == client_request_id,
            )
        )

    def get_execution_required(self, execution_id: str) -> AssistantExecutionRecord:
        record = self.get_execution(execution_id)
        if record is None:
            raise LookupError(f"Assistant execution not found: {execution_id}")
        return record

    def list_executions(
        self,
        session_id: str,
        *,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AssistantExecutionRecord]:
        statement = select(AssistantExecutionRecord).where(
            AssistantExecutionRecord.session_id == session_id
        )
        if statuses:
            statement = statement.where(AssistantExecutionRecord.status.in_(statuses))
        statement = statement.order_by(
            AssistantExecutionRecord.created_at.desc(),
            AssistantExecutionRecord.id.desc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        statement = statement.offset(max(0, offset))
        return list(self._db_session.scalars(statement))

    def list_action_executions(
        self,
        *,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[AssistantExecutionRecord]:
        statement = select(AssistantExecutionRecord).where(
            AssistantExecutionRecord.profile == "action_run"
        )
        if statuses is not None:
            statement = statement.where(
                AssistantExecutionRecord.status.in_(tuple(statuses))
            )
        statement = statement.order_by(
            AssistantExecutionRecord.created_at,
            AssistantExecutionRecord.id,
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self._db_session.scalars(statement))

    def transition_execution(
        self,
        execution_id: str,
        *,
        expected_version: int,
        target_status: str,
        event_type: str = "execution.status_changed",
        summary: str,
        phase: str | None = None,
        payload: Mapping[str, object] | None = None,
        result: Mapping[str, object] | None = None,
        snapshot_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        changed_at: dt.datetime | None = None,
    ) -> AssistantExecutionRecord:
        record = self.get_execution_required(execution_id)
        if record.state_version != expected_version:
            raise AssistantStateConflictError(
                f"Execution {execution_id} no longer has version {expected_version}"
            )
        next_status = transition_execution_status(
            record.profile,
            record.status,
            target_status,
        )
        timestamp = changed_at or utc_now()
        next_version = expected_version + 1
        terminal = is_terminal_execution_status(record.profile, next_status)
        values: dict[str, object] = {
            "status": next_status,
            "state_version": next_version,
            "updated_at": timestamp,
            "result_json": dict(result) if result is not None else record.result_json,
            "error_code": error_code,
            "error_message": error_message,
            "completed_at": timestamp if terminal else None,
        }
        if snapshot_id is not None:
            values["snapshot_id"] = snapshot_id
        if record.started_at is None and next_status not in {"received", "queued"}:
            values["started_at"] = timestamp
        result_row = self._db_session.execute(
            update(AssistantExecutionRecord)
            .where(
                AssistantExecutionRecord.id == execution_id,
                AssistantExecutionRecord.state_version == expected_version,
                AssistantExecutionRecord.status == record.status,
            )
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        if result_row.rowcount != 1:
            raise AssistantStateConflictError(
                f"Execution {execution_id} changed during transition"
            )
        self._db_session.flush()
        updated = self.get_execution_required(execution_id)
        self.append_event(
            execution_id=execution_id,
            event_type=event_type,
            phase=phase or next_status,
            summary=summary,
            payload=payload or {},
            state_version=next_version,
            created_at=timestamp,
        )
        return updated

    def append_step(
        self,
        *,
        execution_id: str,
        kind: StepKind,
        input_payload: Mapping[str, object],
        output_payload: Mapping[str, object] | None = None,
        decision_summary: str | None = None,
        sequence: int | None = None,
        created_at: dt.datetime | None = None,
    ) -> AssistantStepRecord:
        execution = self.get_execution_required(execution_id)
        next_sequence = sequence or execution.step_count + 1
        if next_sequence < 1:
            raise ValueError("step sequence must be positive")
        timestamp = created_at or utc_now()
        record = AssistantStepRecord(
            id=str(uuid.uuid4()),
            execution_id=execution_id,
            sequence=next_sequence,
            kind=kind,
            input_json=dict(input_payload),
            output_json=(
                dict(output_payload) if output_payload is not None else None
            ),
            decision_summary=decision_summary,
            created_at=timestamp,
        )
        self._db_session.add(record)
        execution.step_count = max(execution.step_count, next_sequence)
        execution.updated_at = timestamp
        self._db_session.flush()
        log_assistant_lifecycle(
            "assistant_action_step",
            session_id=execution.session_id,
            execution_id=execution.id,
            root_execution_id=execution.root_execution_id,
            status=execution.status,
            elapsed_ms=_elapsed_ms(execution.created_at, timestamp),
            profile=execution.profile,
            step_sequence=next_sequence,
            step_kind=kind,
            snapshot_id=execution.snapshot_id,
        )
        return record

    def list_steps(self, execution_id: str, *, limit: int | None = None, offset: int = 0) -> list[AssistantStepRecord]:
        statement = select(AssistantStepRecord).where(
            AssistantStepRecord.execution_id == execution_id).order_by(AssistantStepRecord.sequence)
        if limit is not None:
            if not 1 <= limit <= 101 or offset < 0:
                raise ValueError("invalid step page")
            statement = statement.limit(limit).offset(offset)
        return list(self._db_session.scalars(statement))

    def append_observation(
        self,
        *,
        execution_id: str,
        source: ObservationSource,
        observation: Mapping[str, object],
        evidence_refs: Sequence[str] = (),
        step_id: str | None = None,
        source_ref: str | None = None,
        observation_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> AssistantObservationRecord:
        self.get_execution_required(execution_id)
        record = AssistantObservationRecord(
            id=observation_id or str(uuid.uuid4()),
            execution_id=execution_id,
            step_id=step_id,
            source=source,
            source_ref=source_ref,
            observation_json=dict(observation),
            evidence_refs_json=list(dict.fromkeys(evidence_refs)),
            created_at=created_at or utc_now(),
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def list_observations(
        self,
        execution_id: str,
    ) -> list[AssistantObservationRecord]:
        return list(
            self._db_session.scalars(
                select(AssistantObservationRecord)
                .where(AssistantObservationRecord.execution_id == execution_id)
                .order_by(
                    AssistantObservationRecord.created_at,
                    AssistantObservationRecord.id,
                )
            )
        )

    def append_event(
        self,
        *,
        execution_id: str,
        event_type: str,
        summary: str,
        payload: Mapping[str, object],
        state_version: int | None = None,
        phase: str | None = None,
        schema_version: int = 1,
        created_at: dt.datetime | None = None,
    ) -> AssistantEventRecord:
        execution = self.get_execution_required(execution_id)
        record = AssistantEventRecord(
            session_id=execution.session_id,
            execution_id=execution.id,
            root_execution_id=execution.root_execution_id,
            state_version=state_version or execution.state_version,
            schema_version=schema_version,
            event_type=event_type,
            phase=phase,
            status=execution.status,
            summary=summary,
            payload_json=dict(payload),
            created_at=created_at or utc_now(),
        )
        self._db_session.add(record)
        self._db_session.flush()
        lifecycle_event = _assistant_lifecycle_event(
            event_type,
            status=execution.status,
        )
        if lifecycle_event is not None:
            log_assistant_lifecycle(
                lifecycle_event,
                session_id=execution.session_id,
                execution_id=execution.id,
                root_execution_id=execution.root_execution_id,
                status=execution.status,
                elapsed_ms=_elapsed_ms(execution.created_at, record.created_at),
                profile=execution.profile,
                source_event=event_type,
                snapshot_id=execution.snapshot_id,
                state_version=record.state_version,
                handoff_id=(
                    record.payload_json.get("handoff_id")
                    if isinstance(record.payload_json.get("handoff_id"), str)
                    else None
                ),
                target_execution_id=(
                    record.payload_json.get("target_execution_id")
                    if isinstance(
                        record.payload_json.get("target_execution_id"),
                        str,
                    )
                    else None
                ),
            )
        return record

    def create_grant(
        self,
        *,
        session_id: str,
        actor_id: str,
        goal: str,
        capabilities: Sequence[str],
        resource_scope: Mapping[str, object],
        candidate_ids: Sequence[str],
        max_side_effects: int,
        expires_at: dt.datetime,
        linear_team_id: str | None = None,
        unresolved_identity_policy: str = "placeholder",
        grant_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> ActionGrantRecord:
        if max_side_effects < 0:
            raise ValueError("max_side_effects must be non-negative")
        timestamp = created_at or utc_now()
        record = ActionGrantRecord(
            id=grant_id or str(uuid.uuid4()),
            session_id=session_id,
            actor_id=actor_id,
            goal=goal,
            capabilities_json=list(dict.fromkeys(capabilities)),
            resource_scope_json=dict(resource_scope),
            candidate_ids_json=list(dict.fromkeys(candidate_ids)),
            linear_team_id=linear_team_id,
            max_side_effects=max_side_effects,
            used_side_effects=0,
            unresolved_identity_policy=unresolved_identity_policy,
            expires_at=expires_at,
            status="active",
            created_at=timestamp,
            updated_at=timestamp,
            revoked_at=None,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def get_grant(self, grant_id: str) -> ActionGrantRecord | None:
        return self._db_session.get(ActionGrantRecord, grant_id)

    def consume_grant(
        self,
        grant_id: str,
        *,
        side_effects: int = 1,
        consumed_at: dt.datetime | None = None,
    ) -> ActionGrantRecord:
        if side_effects < 1:
            raise ValueError("side_effects must be positive")
        record = self._db_session.get(ActionGrantRecord, grant_id)
        if record is None:
            raise LookupError(f"Action Grant not found: {grant_id}")
        timestamp = consumed_at or utc_now()
        next_used = record.used_side_effects + side_effects
        if (
            record.status != "active"
            or _as_utc(record.expires_at) <= _as_utc(timestamp)
            or next_used > record.max_side_effects
        ):
            raise ActionGrantUnavailableError(f"Action Grant is unavailable: {grant_id}")
        result = self._db_session.execute(
            update(ActionGrantRecord)
            .where(
                ActionGrantRecord.id == grant_id,
                ActionGrantRecord.status == "active",
                ActionGrantRecord.used_side_effects == record.used_side_effects,
                ActionGrantRecord.expires_at > timestamp,
            )
            .values(
                used_side_effects=next_used,
                status=(
                    "consumed"
                    if next_used == record.max_side_effects
                    else "active"
                ),
                updated_at=timestamp,
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise ActionGrantUnavailableError(f"Action Grant changed: {grant_id}")
        self._db_session.flush()
        updated = self._db_session.get(ActionGrantRecord, grant_id)
        if updated is None:
            raise LookupError(f"Action Grant not found: {grant_id}")
        return updated

    def release_grant_side_effect(
        self,
        grant_id: str,
        *,
        side_effects: int = 1,
        released_at: dt.datetime | None = None,
    ) -> ActionGrantRecord:
        if side_effects < 1:
            raise ValueError("side_effects must be positive")
        record = self._db_session.get(ActionGrantRecord, grant_id)
        if record is None:
            raise LookupError(f"Action Grant not found: {grant_id}")
        if record.used_side_effects < side_effects:
            raise ActionGrantUnavailableError(
                f"Action Grant has no reserved side effect: {grant_id}"
            )
        timestamp = released_at or utc_now()
        next_used = record.used_side_effects - side_effects
        next_status = (
            "expired"
            if _as_utc(record.expires_at) <= _as_utc(timestamp)
            else "active"
        )
        result = self._db_session.execute(
            update(ActionGrantRecord)
            .where(
                ActionGrantRecord.id == grant_id,
                ActionGrantRecord.status.in_(("active", "consumed")),
                ActionGrantRecord.used_side_effects == record.used_side_effects,
            )
            .values(
                used_side_effects=next_used,
                status=next_status,
                updated_at=timestamp,
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise ActionGrantUnavailableError(f"Action Grant changed: {grant_id}")
        self._db_session.flush()
        updated = self._db_session.get(ActionGrantRecord, grant_id)
        if updated is None:
            raise LookupError(f"Action Grant not found: {grant_id}")
        return updated

    def transition_grant_status(
        self,
        grant_id: str,
        target: GrantStatus,
        *,
        expected_status: GrantStatus | None = None,
        changed_at: dt.datetime | None = None,
    ) -> ActionGrantRecord:
        if target not in {"revoked", "expired"}:
            raise ValueError("Grant status changes must use consume/release or revoke/expire")
        record = self._db_session.get(ActionGrantRecord, grant_id)
        if record is None:
            raise LookupError(f"Action Grant not found: {grant_id}")
        if expected_status is not None and record.status != expected_status:
            raise ActionGrantUnavailableError(f"Action Grant changed: {grant_id}")
        if record.status in {"revoked", "expired"}:
            if record.status == target:
                return record
            raise ActionGrantUnavailableError(f"Action Grant is terminal: {grant_id}")
        timestamp = changed_at or utc_now()
        previous_status = record.status
        changed = self._db_session.execute(
            update(ActionGrantRecord)
            .where(
                ActionGrantRecord.id == grant_id,
                ActionGrantRecord.status == previous_status,
            )
            .values(
                status=target,
                updated_at=timestamp,
                revoked_at=timestamp if target == "revoked" else record.revoked_at,
            )
            .execution_options(synchronize_session="fetch")
        )
        if changed.rowcount != 1:
            raise ActionGrantUnavailableError(f"Action Grant changed: {grant_id}")
        self._db_session.flush()
        updated = self._db_session.get(ActionGrantRecord, grant_id)
        if updated is None:
            raise LookupError(f"Action Grant not found: {grant_id}")
        return updated

    def revoke_grant(self, grant_id: str, *, expected_status: GrantStatus | None = None,
                     changed_at: dt.datetime | None = None) -> ActionGrantRecord:
        return self.transition_grant_status(
            grant_id, "revoked", expected_status=expected_status, changed_at=changed_at
        )

    def expire_grant(self, grant_id: str, *, expected_status: GrantStatus | None = None,
                     changed_at: dt.datetime | None = None) -> ActionGrantRecord:
        return self.transition_grant_status(
            grant_id, "expired", expected_status=expected_status, changed_at=changed_at
        )

    def prepare_tool_call(
        self,
        *,
        execution_id: str,
        tool_name: str,
        tool_version: str,
        capability: str,
        effect: ToolEffect,
        arguments: Mapping[str, object],
        arguments_hash: str,
        logical_action_key: str | None = None,
        idempotency_key: str | None = None,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> AssistantToolCallRecord:
        _require_digest(arguments_hash, "arguments_hash")
        self.get_execution_required(execution_id)
        if idempotency_key is not None:
            existing = self._db_session.scalar(
                select(AssistantToolCallRecord).where(
                    AssistantToolCallRecord.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.arguments_hash != arguments_hash:
                    raise AssistantIdempotencyConflictError(
                        "idempotency key is already bound to different arguments"
                    )
                return existing
        timestamp = created_at or utc_now()
        record = AssistantToolCallRecord(
            id=tool_call_id or str(uuid.uuid4()),
            execution_id=execution_id,
            step_id=step_id,
            tool_name=tool_name,
            tool_version=tool_version,
            capability=capability,
            effect=effect,
            arguments_json=dict(arguments),
            arguments_hash=arguments_hash,
            logical_action_key=logical_action_key,
            idempotency_key=idempotency_key,
            status="prepared",
            external_reference_json=None,
            result_json=None,
            error_code=None,
            error_message=None,
            attempt_count=0,
            created_at=timestamp,
            updated_at=timestamp,
            requested_at=None,
            completed_at=None,
        )
        try:
            with self._db_session.begin_nested():
                self._db_session.add(record)
                self._db_session.flush()
            execution = self.get_execution_required(execution_id)
            log_assistant_lifecycle(
                "assistant_tool_prepared",
                session_id=execution.session_id,
                execution_id=execution.id,
                root_execution_id=execution.root_execution_id,
                status=record.status,
                elapsed_ms=_elapsed_ms(execution.created_at, timestamp),
                profile=execution.profile,
                snapshot_id=execution.snapshot_id,
                tool_call_id=record.id,
                tool_name=record.tool_name,
                capability=record.capability,
            )
            return record
        except IntegrityError:
            if idempotency_key is None:
                raise
            existing = self._db_session.scalar(
                select(AssistantToolCallRecord).where(
                    AssistantToolCallRecord.idempotency_key == idempotency_key
                )
            )
            if existing is None:
                raise
            if existing.arguments_hash != arguments_hash:
                raise AssistantIdempotencyConflictError(
                    "idempotency key is already bound to different arguments"
                )
            return existing

    def get_tool_call(
        self,
        tool_call_id: str,
    ) -> AssistantToolCallRecord | None:
        return self._db_session.get(AssistantToolCallRecord, tool_call_id)

    def get_tool_call_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> AssistantToolCallRecord | None:
        return self._db_session.scalar(
            select(AssistantToolCallRecord).where(
                AssistantToolCallRecord.idempotency_key == idempotency_key
            )
        )

    def list_tool_calls(
        self,
        execution_id: str,
        *,
        statuses: Sequence[str] | None = None,
        effect: ToolEffect | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AssistantToolCallRecord]:
        statement = select(AssistantToolCallRecord).where(
            AssistantToolCallRecord.execution_id == execution_id
        )
        if statuses is not None:
            statement = statement.where(
                AssistantToolCallRecord.status.in_(tuple(statuses))
            )
        if effect is not None:
            statement = statement.where(AssistantToolCallRecord.effect == effect)
        if limit is not None:
            if not 1 <= limit <= 101 or offset < 0:
                raise ValueError("invalid tool-call page")
            statement = statement.limit(limit).offset(offset)
        return list(
            self._db_session.scalars(
                statement.order_by(
                    AssistantToolCallRecord.created_at,
                    AssistantToolCallRecord.id,
                )
            )
        )

    def replace_prepared_tool_call_arguments(
        self,
        tool_call_id: str,
        *,
        execution_id: str,
        expected_arguments_hash: str,
        arguments: Mapping[str, object],
        arguments_hash: str,
        changed_at: dt.datetime | None = None,
    ) -> AssistantToolCallRecord:
        _require_digest(expected_arguments_hash, "expected_arguments_hash")
        _require_digest(arguments_hash, "arguments_hash")
        timestamp = changed_at or utc_now()
        result = self._db_session.execute(
            update(AssistantToolCallRecord)
            .where(
                AssistantToolCallRecord.id == tool_call_id,
                AssistantToolCallRecord.execution_id == execution_id,
                AssistantToolCallRecord.status == "prepared",
                AssistantToolCallRecord.arguments_hash
                == expected_arguments_hash,
            )
            .values(
                arguments_json=dict(arguments),
                arguments_hash=arguments_hash,
                updated_at=timestamp,
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise AssistantStateConflictError(
                "prepared ToolCall changed before its arguments were replaced"
            )
        self._db_session.flush()
        updated = self.get_tool_call(tool_call_id)
        if updated is None:
            raise LookupError(f"Assistant ToolCall not found: {tool_call_id}")
        return updated

    def update_tool_call(
        self,
        tool_call_id: str,
        *,
        expected_status: ToolCallStatus | None = None,
        status: ToolCallStatus,
        result: Mapping[str, object] | None = None,
        external_reference: Mapping[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
        changed_at: dt.datetime | None = None,
    ) -> AssistantToolCallRecord:
        record = self._db_session.get(AssistantToolCallRecord, tool_call_id)
        if record is None:
            raise LookupError(f"Assistant ToolCall not found: {tool_call_id}")
        if expected_status is not None and record.status != expected_status:
            raise AssistantStateConflictError(
                f"ToolCall no longer has status {expected_status}"
            )
        timestamp = changed_at or utc_now()
        previous_status = record.status
        next_status = transition_tool_call_status(previous_status, status)
        terminal = previous_status in {"succeeded", "failed"}
        if terminal:
            unchanged = (
                next_status == previous_status
                and not increment_attempt
                and (result is None or dict(result) == record.result_json)
                and (external_reference is None or dict(external_reference) == record.external_reference_json)
                and (error_code is None or error_code == record.error_code)
                and (error_message is None or error_message == record.error_message)
            )
            if unchanged:
                return record
            raise AssistantStateConflictError("terminal ToolCall cannot be changed")
        values = {
            "status": next_status,
            "result_json": dict(result) if result is not None else record.result_json,
            "external_reference_json": (
                dict(external_reference)
                if external_reference is not None
                else record.external_reference_json
            ),
            "error_code": error_code if error_code is not None else record.error_code,
            "error_message": error_message if error_message is not None else record.error_message,
            "attempt_count": record.attempt_count + (1 if increment_attempt else 0),
            "requested_at": (
                timestamp
                if next_status == "requesting" and record.requested_at is None
                else record.requested_at
            ),
            "completed_at": timestamp if next_status in {"succeeded", "failed"} else record.completed_at,
            "updated_at": timestamp,
        }
        updated_count = self._db_session.execute(
            update(AssistantToolCallRecord)
            .where(
                AssistantToolCallRecord.id == tool_call_id,
                AssistantToolCallRecord.status == previous_status,
            )
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        if updated_count.rowcount != 1:
            raise AssistantStateConflictError("ToolCall changed concurrently")
        self._db_session.flush()
        record = self._db_session.get(AssistantToolCallRecord, tool_call_id)
        if record is None:
            raise LookupError(f"Assistant ToolCall not found: {tool_call_id}")
        if previous_status == "reconciling":
            execution = self.get_execution_required(record.execution_id)
            log_assistant_lifecycle(
                "assistant_tool_reconciled",
                session_id=execution.session_id,
                execution_id=execution.id,
                root_execution_id=execution.root_execution_id,
                status=record.status,
                elapsed_ms=_elapsed_ms(
                    record.requested_at or record.created_at,
                    timestamp,
                ),
                profile=execution.profile,
                snapshot_id=execution.snapshot_id,
                tool_call_id=record.id,
                tool_name=record.tool_name,
                capability=record.capability,
                attempt_count=record.attempt_count,
            )
        return record

    def create_subagent_run(
        self,
        *,
        execution_id: str,
        planning_round: int,
        role: SubagentRole,
        task: Mapping[str, object],
        budget: Mapping[str, object],
        snapshot_id: str | None,
        subagent_run_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> AssistantSubagentRunRecord:
        if planning_round < 1:
            raise ValueError("planning_round must be positive")
        self.get_execution_required(execution_id)
        record = AssistantSubagentRunRecord(
            id=subagent_run_id or str(uuid.uuid4()),
            execution_id=execution_id,
            planning_round=planning_round,
            role=role,
            status="queued",
            snapshot_id=snapshot_id,
            task_json=dict(task),
            budget_json=dict(budget),
            result_json=None,
            error_code=None,
            error_message=None,
            created_at=created_at or utc_now(),
            started_at=None,
            completed_at=None,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def transition_subagent_run(
        self,
        subagent_run_id: str,
        *,
        expected_status: SubagentStatus | None = None,
        status: SubagentStatus,
        result: Mapping[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        changed_at: dt.datetime | None = None,
    ) -> AssistantSubagentRunRecord:
        record = self._db_session.get(AssistantSubagentRunRecord, subagent_run_id)
        if record is None:
            raise LookupError(f"Assistant Subagent run not found: {subagent_run_id}")
        if expected_status is not None and record.status != expected_status:
            raise AssistantStateConflictError(
                f"Subagent no longer has status {expected_status}"
            )
        timestamp = changed_at or utc_now()
        previous_status = record.status
        next_status = transition_subagent_status(previous_status, status)
        if previous_status in {"completed", "failed", "cancelled"}:
            unchanged = (
                next_status == previous_status
                and (result is None or dict(result) == record.result_json)
                and (error_code is None or error_code == record.error_code)
                and (error_message is None or error_message == record.error_message)
            )
            if unchanged:
                return record
            raise AssistantStateConflictError("terminal Subagent cannot be changed")
        changed = self._db_session.execute(
            update(AssistantSubagentRunRecord)
            .where(
                AssistantSubagentRunRecord.id == subagent_run_id,
                AssistantSubagentRunRecord.status == previous_status,
            )
            .values(
                status=next_status,
                result_json=dict(result) if result is not None else record.result_json,
                error_code=error_code,
                error_message=error_message,
                started_at=(
                    timestamp
                    if next_status == "running" and record.started_at is None
                    else record.started_at
                ),
                completed_at=(
                    timestamp
                    if next_status in {"completed", "failed", "cancelled"}
                    else record.completed_at
                ),
            )
            .execution_options(synchronize_session="fetch")
        )
        if changed.rowcount != 1:
            raise AssistantStateConflictError("Subagent changed concurrently")
        self._db_session.flush()
        updated = self._db_session.get(AssistantSubagentRunRecord, subagent_run_id)
        if updated is None:
            raise LookupError(f"Assistant Subagent run not found: {subagent_run_id}")
        return updated

    def list_subagent_runs(
        self,
        execution_id: str,
        *,
        planning_round: int | None = None,
    ) -> list[AssistantSubagentRunRecord]:
        statement = select(AssistantSubagentRunRecord).where(
            AssistantSubagentRunRecord.execution_id == execution_id
        )
        if planning_round is not None:
            statement = statement.where(
                AssistantSubagentRunRecord.planning_round == planning_round
            )
        return list(
            self._db_session.scalars(
                statement.order_by(
                    AssistantSubagentRunRecord.planning_round,
                    AssistantSubagentRunRecord.role,
                )
            )
        )

    def get_subagent_run(
        self,
        subagent_run_id: str,
    ) -> AssistantSubagentRunRecord | None:
        return self._db_session.get(AssistantSubagentRunRecord, subagent_run_id)

    def reserve_external_action_claim(
        self,
        *,
        provider: str,
        capability: str,
        logical_action_key: str,
        holder_execution_id: str,
        arguments_hash: str,
        lease_expires_at: dt.datetime,
        tool_call_id: str | None = None,
        claim_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> ExternalActionClaimRecord:
        _require_digest(arguments_hash, "arguments_hash")
        self.get_execution_required(holder_execution_id)
        existing = self.observe_external_action_claim(
            provider=provider,
            capability=capability,
            logical_action_key=logical_action_key,
        )
        if existing is not None:
            if existing.arguments_hash != arguments_hash:
                raise AssistantIdempotencyConflictError(
                    "logical action key is already bound to different arguments"
                )
            return existing
        timestamp = created_at or utc_now()
        record = ExternalActionClaimRecord(
            id=claim_id or str(uuid.uuid4()),
            provider=provider,
            capability=capability,
            logical_action_key=logical_action_key,
            holder_execution_id=holder_execution_id,
            tool_call_id=tool_call_id,
            status="reserved",
            arguments_hash=arguments_hash,
            external_reference_json=None,
            lease_expires_at=lease_expires_at,
            created_at=timestamp,
            updated_at=timestamp,
        )
        try:
            with self._db_session.begin_nested():
                self._db_session.add(record)
                self._db_session.flush()
            return record
        except IntegrityError:
            existing = self.observe_external_action_claim(
                provider=provider,
                capability=capability,
                logical_action_key=logical_action_key,
            )
            if existing is None:
                raise
            if existing.arguments_hash != arguments_hash:
                raise AssistantIdempotencyConflictError(
                    "logical action key is already bound to different arguments"
                )
            return existing

    def observe_external_action_claim(
        self,
        *,
        provider: str,
        capability: str,
        logical_action_key: str,
    ) -> ExternalActionClaimRecord | None:
        return self._db_session.scalar(
            select(ExternalActionClaimRecord).where(
                ExternalActionClaimRecord.provider == provider,
                ExternalActionClaimRecord.capability == capability,
                ExternalActionClaimRecord.logical_action_key
                == logical_action_key,
            )
        )

    def list_external_action_claims(
        self,
        *,
        statuses: Sequence[ExternalActionClaimStatus] | None = None,
    ) -> list[ExternalActionClaimRecord]:
        statement = select(ExternalActionClaimRecord)
        if statuses is not None:
            statement = statement.where(
                ExternalActionClaimRecord.status.in_(tuple(statuses))
            )
        return list(
            self._db_session.scalars(
                statement.order_by(
                    ExternalActionClaimRecord.created_at,
                    ExternalActionClaimRecord.id,
                )
            )
        )

    def replace_reserved_claim_arguments(
        self,
        claim_id: str,
        *,
        holder_execution_id: str,
        tool_call_id: str,
        expected_arguments_hash: str,
        arguments_hash: str,
        changed_at: dt.datetime | None = None,
    ) -> ExternalActionClaimRecord:
        _require_digest(expected_arguments_hash, "expected_arguments_hash")
        _require_digest(arguments_hash, "arguments_hash")
        timestamp = changed_at or utc_now()
        result = self._db_session.execute(
            update(ExternalActionClaimRecord)
            .where(
                ExternalActionClaimRecord.id == claim_id,
                ExternalActionClaimRecord.holder_execution_id
                == holder_execution_id,
                ExternalActionClaimRecord.tool_call_id == tool_call_id,
                ExternalActionClaimRecord.status == "reserved",
                ExternalActionClaimRecord.arguments_hash
                == expected_arguments_hash,
            )
            .values(
                arguments_hash=arguments_hash,
                updated_at=timestamp,
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise AssistantStateConflictError(
                "reserved external action claim changed before its arguments "
                "were replaced"
            )
        self._db_session.flush()
        updated = self._db_session.get(ExternalActionClaimRecord, claim_id)
        if updated is None:
            raise LookupError(f"External action claim not found: {claim_id}")
        return updated

    def transition_external_action_claim(
        self,
        claim_id: str,
        *,
        holder_execution_id: str,
        expected_status: ExternalActionClaimStatus,
        target_status: ExternalActionClaimStatus,
        arguments_hash: str | None = None,
        external_reference: Mapping[str, object] | None = None,
        lease_expires_at: dt.datetime | None = None,
        changed_at: dt.datetime | None = None,
    ) -> ExternalActionClaimRecord:
        record = self._db_session.get(ExternalActionClaimRecord, claim_id)
        if record is None:
            raise LookupError(f"External action claim not found: {claim_id}")
        if record.holder_execution_id != holder_execution_id:
            raise AssistantStateConflictError("execution does not hold this claim")
        if record.status != expected_status:
            raise AssistantStateConflictError(
                f"claim no longer has status {expected_status}"
            )
        next_status = transition_external_action_claim(
            expected_status,
            target_status,
        )
        next_hash = arguments_hash or record.arguments_hash
        _require_digest(next_hash, "arguments_hash")
        if expected_status != "reserved" and next_hash != record.arguments_hash:
            raise AssistantIdempotencyConflictError(
                "claim arguments cannot change after requesting"
            )
        timestamp = changed_at or utc_now()
        result = self._db_session.execute(
            update(ExternalActionClaimRecord)
            .where(
                ExternalActionClaimRecord.id == claim_id,
                ExternalActionClaimRecord.holder_execution_id
                == holder_execution_id,
                ExternalActionClaimRecord.status == expected_status,
            )
            .values(
                status=next_status,
                arguments_hash=next_hash,
                external_reference_json=(
                    dict(external_reference)
                    if external_reference is not None
                    else record.external_reference_json
                ),
                lease_expires_at=lease_expires_at or record.lease_expires_at,
                updated_at=timestamp,
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise AssistantStateConflictError("external action claim changed")
        self._db_session.flush()
        updated = self._db_session.get(ExternalActionClaimRecord, claim_id)
        if updated is None:
            raise LookupError(f"External action claim not found: {claim_id}")
        return updated

    def reconcile_external_action_claim(
        self,
        claim_id: str,
        *,
        holder_execution_id: str,
        succeeded: bool,
        external_reference: Mapping[str, object] | None = None,
        changed_at: dt.datetime | None = None,
    ) -> ExternalActionClaimRecord:
        return self.transition_external_action_claim(
            claim_id,
            holder_execution_id=holder_execution_id,
            expected_status="unknown",
            target_status="succeeded" if succeeded else "failed_safe",
            external_reference=external_reference,
            changed_at=changed_at,
        )

    def create_handoff(
        self,
        envelope: HandoffEnvelope,
        *,
        created_at: dt.datetime | None = None,
    ) -> AssistantHandoffRecord:
        existing = self._db_session.scalar(
            select(AssistantHandoffRecord).where(
                AssistantHandoffRecord.source_execution_id
                == envelope.parent_execution_id
            )
        )
        if existing is not None:
            if existing.target_execution_id != envelope.target_execution_id:
                raise AssistantIdempotencyConflictError(
                    "source execution is already handed off to another target"
                )
            return existing
        record = AssistantHandoffRecord(
            id=envelope.handoff_id,
            source_execution_id=envelope.parent_execution_id,
            target_execution_id=envelope.target_execution_id,
            snapshot_id=envelope.context_snapshot_id,
            grant_id=envelope.grant_id,
            envelope_json=envelope.model_dump(mode="json"),
            created_at=created_at or utc_now(),
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def get_handoff_by_source(
        self,
        source_execution_id: str,
    ) -> AssistantHandoffRecord | None:
        return self._db_session.scalar(
            select(AssistantHandoffRecord).where(
                AssistantHandoffRecord.source_execution_id
                == source_execution_id
            )
        )

    def record_client_operation(
        self,
        *,
        execution_id: str,
        client_operation_id: str,
        kind: ClientOperationKind,
        request_hash: str,
        response: Mapping[str, object],
        created_at: dt.datetime | None = None,
    ) -> AssistantClientOperationRecord:
        _require_digest(request_hash, "request_hash")
        existing = self._db_session.scalar(
            select(AssistantClientOperationRecord).where(
                AssistantClientOperationRecord.execution_id == execution_id,
                AssistantClientOperationRecord.client_operation_id
                == client_operation_id,
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash or existing.kind != kind:
                raise AssistantIdempotencyConflictError(
                    "client operation ID is bound to another request"
                )
            return existing
        record = AssistantClientOperationRecord(
            id=str(uuid.uuid4()),
            execution_id=execution_id,
            client_operation_id=client_operation_id,
            kind=kind,
            request_hash=request_hash,
            response_json=dict(response),
            created_at=created_at or utc_now(),
        )
        try:
            with self._db_session.begin_nested():
                self._db_session.add(record)
                self._db_session.flush()
            return record
        except IntegrityError:
            existing = self.get_client_operation(
                execution_id=execution_id,
                client_operation_id=client_operation_id,
            )
            if existing is None:
                raise
            if existing.request_hash != request_hash or existing.kind != kind:
                raise AssistantIdempotencyConflictError(
                    "client operation ID is bound to another request"
                )
            return existing

    def get_client_operation(
        self,
        *,
        execution_id: str,
        client_operation_id: str,
    ) -> AssistantClientOperationRecord | None:
        return self._db_session.scalar(
            select(AssistantClientOperationRecord).where(
                AssistantClientOperationRecord.execution_id == execution_id,
                AssistantClientOperationRecord.client_operation_id
                == client_operation_id,
            )
        )

    def current_event_cursor(self, session_id: str | None = None) -> int:
        statement = select(func.max(AssistantEventRecord.id))
        if session_id is not None:
            statement = statement.where(AssistantEventRecord.session_id == session_id)
        return int(
            self._db_session.scalar(statement)
            or 0
        )

    def has_session_events_after(self, session_id: str, cursor: int) -> bool:
        return (
            self._db_session.scalar(
                select(AssistantEventRecord.id)
                .where(
                    AssistantEventRecord.session_id == session_id,
                    AssistantEventRecord.id > cursor,
                )
                .limit(1)
            )
            is not None
        )

    def list_session_events(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[AssistantEventRecord]:
        bounded_limit = max(1, min(limit, 100))
        return list(
            self._db_session.scalars(
                select(AssistantEventRecord)
                .where(
                    AssistantEventRecord.session_id == session_id,
                    AssistantEventRecord.id > after,
                )
                .order_by(AssistantEventRecord.id)
                .limit(bounded_limit)
            )
        )
