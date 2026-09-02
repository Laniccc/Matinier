"""Canonical, redacted Agent trace reconstructed from durable records."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.state_machine import is_terminal_execution_status
from app.persistence.models import (
    ActionCandidateRecord,
    ActionGrantRecord,
    AssistantContextSnapshotRecord,
    AssistantEventRecord,
    AssistantExecutionRecord,
    AssistantHandoffRecord,
    AssistantObservationRecord,
    AssistantStepRecord,
    AssistantSubagentRunRecord,
    AssistantToolCallRecord,
    ExternalActionClaimRecord,
    MediaSessionRecord,
    utc_now,
)


TraceEdgeKind = Literal[
    "derived_from", "parent_of", "handed_off_to", "invoked", "authorized_by",
    "claimed_by", "reconciled_to", "terminated_as",
]


class FrozenTraceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TraceNode(FrozenTraceModel):
    node_id: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=64)
    timestamp: dt.datetime
    state_version: int | None = Field(default=None, ge=1)
    data: dict[str, Any] = Field(default_factory=dict)


class TraceEdge(FrozenTraceModel):
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)
    kind: TraceEdgeKind


class AgentTrace(FrozenTraceModel):
    schema_version: Literal[1] = 1
    trace_id: str = Field(min_length=1, max_length=36)
    root_execution_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    media_session_id: str | None = Field(default=None, max_length=36)
    generated_at: dt.datetime
    nodes: tuple[TraceNode, ...]
    edges: tuple[TraceEdge, ...]


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _external_reference(value: dict[str, object] | None) -> dict[str, object] | None:
    if not value:
        return None
    return {
        key: value[key]
        for key in ("identifier", "task_ref", "url", "action_key", "job_id")
        if isinstance(value.get(key), (str, int, float, bool))
    } or None


def export_agent_trace(db: Session, execution_id: str, *, media_session_id: str | None = None,
                       generated_at: dt.datetime | None = None) -> AgentTrace:
    selected = db.get(AssistantExecutionRecord, execution_id)
    if selected is None:
        raise LookupError(f"Assistant execution not found: {execution_id}")
    root_id = selected.root_execution_id
    executions = list(db.scalars(select(AssistantExecutionRecord).where(
        AssistantExecutionRecord.root_execution_id == root_id,
    )))
    if not executions:
        raise LookupError(f"Assistant execution root not found: {root_id}")
    execution_ids = {row.id for row in executions}
    session_id = selected.session_id
    if media_session_id is None:
        media = db.scalar(select(MediaSessionRecord).where(
            MediaSessionRecord.legacy_session_id == session_id,
        ))
        media_session_id = media.id if media is not None else None

    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    node_ids: set[str] = set()

    def add(node_id: str, kind: str, timestamp: dt.datetime, data=None, state_version=None):
        if node_id in node_ids:
            return
        node_ids.add(node_id)
        nodes.append(TraceNode(node_id=node_id, kind=kind, timestamp=timestamp,
            state_version=state_version, data=dict(data or {})))

    def edge(source: str, target: str, kind: TraceEdgeKind):
        edges.append(TraceEdge(source=source, target=target, kind=kind))

    snapshot_ids = {row.snapshot_id for row in executions if row.snapshot_id}
    snapshots = [row for sid in snapshot_ids if (row := db.get(AssistantContextSnapshotRecord, sid)) is not None]
    evidence_nodes: dict[str, str] = {}
    for snapshot in snapshots:
        context_id = f"context:{snapshot.id}"
        add(context_id, "context", snapshot.created_at, {
            "snapshot_id": snapshot.id,
            "meeting_state_version": snapshot.meeting_state_version,
            "relevant_context_hash": snapshot.relevant_context_hash,
            "evidence_count": len(snapshot.evidence_refs_json),
            "state_slice_hash": _hash(snapshot.state_slice_json),
        })
        messages = {
            value.get("message_id"): value
            for value in snapshot.evidence_messages_json
            if isinstance(value, dict) and isinstance(value.get("message_id"), str)
        }
        for evidence_ref in snapshot.evidence_refs_json:
            message = messages.get(evidence_ref, {})
            anchor_id = f"evidence:{evidence_ref}"
            evidence_nodes[evidence_ref] = anchor_id
            add(anchor_id, "input_anchor", snapshot.created_at, {
                "anchor_type": message.get("message_kind", "evidence"),
                "evidence_id": evidence_ref,
                "segment_id": message.get("segment_id"),
                "revision": message.get("segment_revision"),
                "audio_start_ms": message.get("audio_start_ms"),
                "audio_end_ms": message.get("audio_end_ms"),
            })
            edge(context_id, anchor_id, "derived_from")

    execution_by_id = {row.id: row for row in executions}
    for row in executions:
        node_id = f"execution:{row.id}"
        add(node_id, "execution", row.created_at, {
            "execution_id": row.id,
            "root_execution_id": row.root_execution_id,
            "parent_execution_id": row.parent_execution_id,
            "profile": row.profile,
            "status": row.status,
            "state_version": row.state_version,
            "goal_hash": _hash(row.goal),
            "goal_chars": len(row.goal),
            "snapshot_id": row.snapshot_id,
            "grant_id": row.grant_id,
            "error_code": row.error_code,
        }, state_version=row.state_version)
        if row.snapshot_id:
            edge(node_id, f"context:{row.snapshot_id}", "derived_from")
        if row.parent_execution_id:
            edge(f"execution:{row.parent_execution_id}", node_id, "parent_of")

    events = list(db.scalars(select(AssistantEventRecord).where(
        AssistantEventRecord.root_execution_id == root_id,
    ).order_by(AssistantEventRecord.created_at, AssistantEventRecord.state_version, AssistantEventRecord.id)))
    for event in events:
        kind = "recovery" if any(part in event.event_type for part in ("recover", "reconcil")) else "execution_event"
        event_id = f"event:{event.id}"
        add(event_id, kind, event.created_at, {
            "event_id": event.id,
            "event_type": event.event_type,
            "execution_id": event.execution_id,
            "status": event.status,
            "phase": event.phase,
            "payload_hash": _hash(event.payload_json),
        }, state_version=event.state_version)
        edge(event_id, f"execution:{event.execution_id}", "derived_from")

    stages_by_execution: dict[str, list[str]] = defaultdict(list)
    steps = list(db.scalars(select(AssistantStepRecord).where(
        AssistantStepRecord.execution_id.in_(execution_ids),
    )))
    for step in steps:
        node_id = f"model-stage:{step.id}"
        add(node_id, "model_stage", step.created_at, {
            "execution_id": step.execution_id,
            "sequence": step.sequence,
            "stage_kind": step.kind,
            "input_hash": _hash(step.input_json),
            "output_hash": _hash(step.output_json),
        })
        stages_by_execution[step.execution_id].append(node_id)
        edge(node_id, f"execution:{step.execution_id}", "derived_from")

    subagents = list(db.scalars(select(AssistantSubagentRunRecord).where(
        AssistantSubagentRunRecord.execution_id.in_(execution_ids),
    )))
    for branch in subagents:
        add(f"subagent:{branch.id}", "subagent", branch.created_at, {
            "execution_id": branch.execution_id,
            "planning_round": branch.planning_round,
            "role": branch.role,
            "status": branch.status,
            "snapshot_id": branch.snapshot_id,
            "task_hash": _hash(branch.task_json),
            "budget_hash": _hash(branch.budget_json),
            "error_code": branch.error_code,
        })
        edge(f"subagent:{branch.id}", f"execution:{branch.execution_id}", "derived_from")

    handoffs = list(db.scalars(select(AssistantHandoffRecord).where(
        AssistantHandoffRecord.source_execution_id.in_(execution_ids),
    )))
    for handoff in handoffs:
        node_id = f"handoff:{handoff.id}"
        add(node_id, "handoff", handoff.created_at, {
            "handoff_id": handoff.id,
            "source_execution_id": handoff.source_execution_id,
            "target_execution_id": handoff.target_execution_id,
            "snapshot_id": handoff.snapshot_id,
            "grant_id": handoff.grant_id,
            "envelope_hash": _hash(handoff.envelope_json),
        })
        edge(node_id, f"execution:{handoff.source_execution_id}", "derived_from")
        edge(f"execution:{handoff.source_execution_id}", f"execution:{handoff.target_execution_id}", "handed_off_to")

    grant_ids = {row.grant_id for row in executions if row.grant_id}
    grants = [row for gid in grant_ids if (row := db.get(ActionGrantRecord, gid)) is not None]
    candidate_node_ids: dict[str, str] = {}
    for grant in grants:
        add(f"grant:{grant.id}", "grant", grant.created_at, {
            "grant_id": grant.id,
            "status": grant.status,
            "capabilities": sorted(grant.capabilities_json),
            "candidate_ids": sorted(grant.candidate_ids_json),
            "max_side_effects": grant.max_side_effects,
            "used_side_effects": grant.used_side_effects,
            "scope_hash": _hash(grant.resource_scope_json),
        })
        for candidate_id in grant.candidate_ids_json:
            candidate = db.get(ActionCandidateRecord, candidate_id)
            candidate_node_id = f"candidate:{candidate_id}"
            candidate_node_ids[candidate_id] = candidate_node_id
            add(candidate_node_id, "input_anchor", grant.created_at, {
                "anchor_type": "candidate",
                "candidate_id": candidate_id,
                "revision": candidate.current_revision if candidate is not None else None,
                "lineage_root_id": candidate.lineage_root_id if candidate is not None else None,
            })
            edge(f"grant:{grant.id}", candidate_node_id, "authorized_by")

    observations = list(db.scalars(select(AssistantObservationRecord).where(
        AssistantObservationRecord.execution_id.in_(execution_ids),
    )))
    evidence_by_source = defaultdict(set)
    for observation in observations:
        if observation.source_ref:
            evidence_by_source[observation.source_ref].update(observation.evidence_refs_json)

    tools = list(db.scalars(select(AssistantToolCallRecord).where(
        AssistantToolCallRecord.execution_id.in_(execution_ids),
    )))
    tools_by_execution: dict[str, list[str]] = defaultdict(list)
    tool_by_id = {row.id: row for row in tools}
    for call in tools:
        external = _external_reference(call.external_reference_json)
        node_id = f"tool:{call.id}"
        add(node_id, "tool", call.created_at, {
            "tool_call_id": call.id,
            "execution_id": call.execution_id,
            "tool_name": call.tool_name,
            "tool_version": call.tool_version,
            "capability": call.capability,
            "effect": call.effect,
            "status": call.status,
            "arguments_hash": call.arguments_hash,
            "argument_keys": sorted(call.arguments_json),
            "argument_value_hashes": {
                key: _hash(value) for key, value in sorted(call.arguments_json.items())
            },
            "logical_action_key": call.logical_action_key,
            "idempotency_key": call.idempotency_key,
            "external_reference": external,
            "attempt_count": call.attempt_count,
            "create_attempt_count": 1 if call.requested_at is not None else 0,
            "reconcile_attempt_count": max(0, call.attempt_count - (1 if call.requested_at is not None else 0)),
            "error_code": call.error_code,
        })
        tools_by_execution[call.execution_id].append(node_id)
        edge(f"execution:{call.execution_id}", node_id, "invoked")
        edge(node_id, f"execution:{call.execution_id}", "derived_from")
        execution = execution_by_id[call.execution_id]
        if execution.grant_id:
            edge(node_id, f"grant:{execution.grant_id}", "authorized_by")
        for ref in evidence_by_source.get(call.id, ()):
            if ref in evidence_nodes:
                edge(node_id, evidence_nodes[ref], "derived_from")

    claims = list(db.scalars(select(ExternalActionClaimRecord).where(
        ExternalActionClaimRecord.holder_execution_id.in_(execution_ids),
    )))
    for claim in claims:
        node_id = f"claim:{claim.id}"
        add(node_id, "claim", claim.created_at, {
            "claim_id": claim.id,
            "execution_id": claim.holder_execution_id,
            "tool_call_id": claim.tool_call_id,
            "provider": claim.provider,
            "capability": claim.capability,
            "status": claim.status,
            "logical_action_key": claim.logical_action_key,
            "arguments_hash": claim.arguments_hash,
            "external_reference": _external_reference(claim.external_reference_json),
        })
        edge(node_id, f"execution:{claim.holder_execution_id}", "derived_from")
        if claim.tool_call_id in tool_by_id:
            edge(f"tool:{claim.tool_call_id}", node_id, "claimed_by")
            if tool_by_id[claim.tool_call_id].status == "succeeded" and claim.status == "succeeded":
                edge(node_id, f"tool:{claim.tool_call_id}", "reconciled_to")

    parent_ids = {row.parent_execution_id for row in executions if row.parent_execution_id}
    leaf_terminals = [row for row in executions if row.id not in parent_ids and (
        is_terminal_execution_status(row.profile, row.status) or row.status == "needs_input"
    )]
    for terminal in leaf_terminals:
        node_id = f"terminal:{terminal.id}"
        timestamp = terminal.completed_at or terminal.updated_at
        add(node_id, "terminal", timestamp, {
            "execution_id": terminal.id,
            "status": terminal.status,
            "result_hash": _hash(terminal.result_json),
            "error_code": terminal.error_code,
        }, state_version=terminal.state_version)
        edge(f"execution:{terminal.id}", node_id, "terminated_as")
        lineage = tools_by_execution.get(terminal.id) or stages_by_execution.get(terminal.id) or [f"execution:{terminal.id}"]
        edge(node_id, lineage[-1], "derived_from")

    def time_key(value):
        timestamp = value.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.UTC)
        return (timestamp.astimezone(dt.UTC), value.state_version or 0, value.node_id)

    nodes.sort(key=time_key)
    edges = sorted(set(edges), key=lambda value: (value.source, value.target, value.kind))
    return AgentTrace(
        trace_id=root_id,
        root_execution_id=root_id,
        session_id=session_id,
        media_session_id=media_session_id,
        generated_at=generated_at or utc_now(),
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


__all__ = ["AgentTrace", "TraceEdge", "TraceEdgeKind", "TraceNode", "export_agent_trace"]
