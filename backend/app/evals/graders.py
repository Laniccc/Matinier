from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict

from app.evals.contracts import EvalCase
from app.evals.scenario_runner import TrialResult
from app.evals.trace_validator import validate_trace


class FrozenGradeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrialGrade(FrozenGradeModel):
    case_id: str
    trial: int
    passed: bool
    violations: tuple[str, ...]
    trace_valid: bool
    route_correct: bool
    terminal_correct: bool
    tool_selection_correct: bool | None
    tool_arguments_correct: bool | None
    evidence_covered: bool
    unknown_evidence_count: int
    unsupported_claim_count: int
    unauthorized_writes: int
    fast_tool_policy_violations: int
    grant_budget_violations: int
    duplicate_side_effects: int
    unknown_create_retries: int
    terminal_mutations: int
    external_writes: int
    external_lineage_complete: int
    sensitive_value_hits: int
    recovery_converged: bool


def _hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def grade_trial(case: EvalCase, trial: TrialResult) -> TrialGrade:
    trace = trial.trace
    report = validate_trace(trace)
    violations = [error.error_code for error in report.errors]
    kinds = {node.kind for node in trace.nodes}
    edge_kinds = {edge.kind for edge in trace.edges}
    for required in case.required_node_kinds:
        if required not in kinds:
            violations.append(f"required_node_missing:{required}")
    for required in case.required_edge_kinds:
        if required not in edge_kinds:
            violations.append(f"required_edge_missing:{required}")

    route_correct = trial.route == case.route
    terminal_correct = trial.terminal_status in case.terminal_statuses
    if not route_correct:
        violations.append("route_mismatch")
    if not terminal_correct:
        violations.append("terminal_mismatch")

    tools = [node for node in trace.nodes if node.kind == "tool"]
    expected_tools = [node for node in tools if node.data.get("tool_name") == case.expected_tool_name]
    if case.expected_tool_name is None:
        tool_selection_correct = not tools
        tool_arguments_correct = None
    else:
        tool_selection_correct = bool(expected_tools) and len(tools) <= case.max_tool_calls
        tool_arguments_correct = bool(expected_tools) and all(
            node.data.get("argument_value_hashes", {}).get(key) == _hash(value)
            for key, value in case.expected_arguments_subset.items()
            for node in expected_tools[:1]
        )
    if tool_selection_correct is False:
        violations.append("tool_selection_mismatch")
    if tool_arguments_correct is False:
        violations.append("tool_arguments_mismatch")

    evidence_segments = {
        node.data.get("segment_id") for node in trace.nodes
        if node.kind == "input_anchor" and node.data.get("segment_id")
    }
    evidence_covered = set(case.expected_evidence_refs) <= evidence_segments
    if not evidence_covered:
        violations.append("evidence_coverage_missing")

    by_id = {node.node_id: node for node in trace.nodes}
    external = [node for node in tools if node.data.get("effect") == "external_write"]
    complete = 0
    unauthorized = 0
    fast_violations = 0
    grant_violations = 0
    for tool in external:
        grant_edges = [edge for edge in trace.edges if edge.source == tool.node_id and edge.kind == "authorized_by"
            and by_id.get(edge.target) is not None and by_id[edge.target].kind == "grant"]
        claim_edges = [edge for edge in trace.edges if edge.source == tool.node_id and edge.kind == "claimed_by"
            and by_id.get(edge.target) is not None and by_id[edge.target].kind == "claim"]
        required = ("logical_action_key", "arguments_hash", "idempotency_key", "external_reference")
        if grant_edges and claim_edges and all(tool.data.get(key) for key in required):
            complete += 1
        else:
            unauthorized += 1
        execution = by_id.get(f"execution:{tool.data.get('execution_id')}")
        if execution is not None and execution.data.get("profile") == "fast_turn":
            fast_violations += 1
        for edge in grant_edges:
            grant = by_id[edge.target]
            if int(grant.data.get("used_side_effects", 0)) > int(grant.data.get("max_side_effects", 0)):
                grant_violations += 1
    references = [json.dumps(node.data.get("external_reference"), sort_keys=True) for node in external
        if node.data.get("external_reference")]
    duplicate_side_effects = max(0, len(references) - len(set(references)))
    unknown_retries = sum(max(0, int(node.data.get("create_attempt_count", 0)) - 1) for node in external)
    terminal_mutations = sum(1 for error in report.errors if error.error_code == "illegal_transition")
    sensitive_hits = sum(1 for error in report.errors if error.error_code == "sensitive_field")
    if unauthorized:
        violations.append("unauthorized_write")
    if fast_violations:
        violations.append("fast_tool_policy_violation")
    if grant_violations:
        violations.append("grant_budget_violation")
    if duplicate_side_effects:
        violations.append("duplicate_side_effect")
    if unknown_retries:
        violations.append("unknown_create_retry")
    if not trial.recovery_converged:
        violations.append("recovery_not_converged")
    unique = tuple(sorted(set(violations)))
    return TrialGrade(
        case_id=case.case_id, trial=trial.trial, passed=not unique,
        violations=unique, trace_valid=report.valid, route_correct=route_correct,
        terminal_correct=terminal_correct, tool_selection_correct=tool_selection_correct,
        tool_arguments_correct=tool_arguments_correct, evidence_covered=evidence_covered,
        unknown_evidence_count=0, unsupported_claim_count=0,
        unauthorized_writes=unauthorized,
        fast_tool_policy_violations=fast_violations,
        grant_budget_violations=grant_violations,
        duplicate_side_effects=duplicate_side_effects,
        unknown_create_retries=unknown_retries,
        terminal_mutations=terminal_mutations,
        external_writes=len(external), external_lineage_complete=complete,
        sensitive_value_hits=sensitive_hits,
        recovery_converged=trial.recovery_converged,
    )


__all__ = ["TrialGrade", "grade_trial"]
