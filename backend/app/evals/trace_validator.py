from __future__ import annotations

import json
from collections import defaultdict, deque

from pydantic import BaseModel, ConfigDict

from app.assistant.state_machine import transition_execution_status
from app.assistant.trace import AgentTrace, TraceNode


class FrozenValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TraceValidationError(FrozenValidationModel):
    trace_id: str
    node_id: str
    error_code: str


class TraceValidationReport(FrozenValidationModel):
    trace_id: str
    valid: bool
    errors: tuple[TraceValidationError, ...]


_SENSITIVE_KEYS = {
    "authorization", "password", "secret", "token", "api_key", "cookie",
    "prompt", "raw_text", "display_text", "transcript", "goal", "arguments",
    "evidence_messages", "audio_bytes", "pcm",
}


def _error(trace, node_id, code):
    return TraceValidationError(trace_id=trace.trace_id, node_id=node_id, error_code=code)


def _sensitive(value, key=""):
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE_KEYS or any(part in normalized for part in ("password", "secret", "api_key", "authorization", "cookie")):
        return True
    if isinstance(value, dict):
        return any(_sensitive(child, str(child_key)) for child_key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_sensitive(child) for child in value)
    return False


def validate_trace(value: AgentTrace | dict) -> TraceValidationReport:
    trace = AgentTrace.model_validate(value)
    errors = []
    if trace.trace_id != trace.root_execution_id:
        errors.append(_error(trace, trace.trace_id, "trace_root_mismatch"))
    by_id: dict[str, TraceNode] = {}
    for node in trace.nodes:
        if node.node_id in by_id:
            errors.append(_error(trace, node.node_id, "duplicate_node"))
        by_id[node.node_id] = node
        if _sensitive(node.data):
            errors.append(_error(trace, node.node_id, "sensitive_field"))
    root_node_id = f"execution:{trace.root_execution_id}"
    if root_node_id not in by_id:
        errors.append(_error(trace, root_node_id, "root_execution_missing"))

    adjacency = defaultdict(set)
    edge_set = set()
    for edge in trace.edges:
        edge_set.add((edge.source, edge.target, edge.kind))
        if edge.source not in by_id or edge.target not in by_id:
            errors.append(_error(trace, edge.source, "orphan_edge"))
            continue
        adjacency[edge.source].add(edge.target)
        adjacency[edge.target].add(edge.source)
    if root_node_id in by_id:
        visited = {root_node_id}
        queue = deque([root_node_id])
        while queue:
            current = queue.popleft()
            for target in adjacency[current] - visited:
                visited.add(target)
                queue.append(target)
        for node_id in set(by_id) - visited:
            errors.append(_error(trace, node_id, "orphan_node"))

    executions = {node.data.get("execution_id"): node for node in trace.nodes if node.kind == "execution"}
    for execution_id, node in executions.items():
        if node.data.get("root_execution_id") != trace.root_execution_id:
            errors.append(_error(trace, node.node_id, "execution_root_mismatch"))
        parent = node.data.get("parent_execution_id")
        if parent is not None:
            if parent not in executions:
                errors.append(_error(trace, node.node_id, "parent_missing"))
            elif (f"execution:{parent}", node.node_id, "parent_of") not in edge_set:
                errors.append(_error(trace, node.node_id, "parent_edge_missing"))

    events_by_execution = defaultdict(list)
    for node in trace.nodes:
        if node.kind in {"execution_event", "recovery"} and node.state_version is not None:
            events_by_execution[node.data.get("execution_id")].append(node)
    for execution_id, event_nodes in events_by_execution.items():
        versions = sorted({node.state_version for node in event_nodes})
        if versions and versions != list(range(1, max(versions) + 1)):
            errors.append(_error(trace, f"execution:{execution_id}", "state_version_gap"))
        statuses = []
        for version in versions:
            same = [node for node in event_nodes if node.state_version == version]
            status = same[-1].data.get("status")
            if isinstance(status, str):
                statuses.append(status)
        profile = executions.get(execution_id).data.get("profile") if execution_id in executions else None
        if profile in {"fast_turn", "action_run"}:
            for current, target in zip(statuses, statuses[1:]):
                try:
                    transition_execution_status(profile, current, target)
                except ValueError:
                    errors.append(_error(trace, f"execution:{execution_id}", "illegal_transition"))
                    break

    for node in trace.nodes:
        if node.kind == "handoff":
            source = f"execution:{node.data.get('source_execution_id')}"
            target = f"execution:{node.data.get('target_execution_id')}"
            if (source, target, "handed_off_to") not in edge_set:
                errors.append(_error(trace, node.node_id, "handoff_pair_missing"))
        if node.kind == "tool":
            execution = f"execution:{node.data.get('execution_id')}"
            if execution not in by_id or (execution, node.node_id, "invoked") not in edge_set:
                errors.append(_error(trace, node.node_id, "tool_execution_missing"))
            if node.data.get("effect") == "external_write":
                required = ("logical_action_key", "arguments_hash", "idempotency_key", "external_reference")
                if any(not node.data.get(key) for key in required):
                    errors.append(_error(trace, node.node_id, "external_lineage_incomplete"))
                if not any(edge.source == node.node_id and edge.kind == "authorized_by" and by_id.get(edge.target, None) and by_id[edge.target].kind == "grant" for edge in trace.edges):
                    errors.append(_error(trace, node.node_id, "grant_edge_missing"))
                if not any(edge.source == node.node_id and edge.kind == "claimed_by" and by_id.get(edge.target, None) and by_id[edge.target].kind == "claim" for edge in trace.edges):
                    errors.append(_error(trace, node.node_id, "claim_edge_missing"))
                if node.data.get("create_attempt_count", 0) > 1:
                    errors.append(_error(trace, node.node_id, "unknown_create_retry"))

    terminals = [node for node in trace.nodes if node.kind == "terminal"]
    if len(terminals) != 1:
        errors.append(_error(trace, trace.trace_id, "terminal_count_invalid"))
    elif not any(by_id[node_id].kind == "input_anchor" and by_id[node_id].data.get("anchor_type") != "candidate"
                 for node_id in _reachable(terminals[0].node_id, adjacency) if node_id in by_id):
        errors.append(_error(trace, terminals[0].node_id, "evidence_path_missing"))

    unique = {(item.node_id, item.error_code): item for item in errors}
    ordered = tuple(unique[key] for key in sorted(unique))
    return TraceValidationReport(trace_id=trace.trace_id, valid=not ordered, errors=ordered)


def _reachable(start, adjacency):
    visited = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in adjacency[current] - visited:
            visited.add(target)
            queue.append(target)
    return visited


__all__ = ["TraceValidationError", "TraceValidationReport", "validate_trace"]
