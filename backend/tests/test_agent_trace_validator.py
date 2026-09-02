import copy
import datetime as dt

import pytest

from app.assistant.trace import AgentTrace, TraceEdge, TraceNode
from app.evals.trace_validator import validate_trace


NOW = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def node(node_id, kind, seconds, data, version=None):
    return TraceNode(node_id=node_id, kind=kind,
        timestamp=NOW + dt.timedelta(seconds=seconds), state_version=version, data=data)


def golden_trace(route="fast"):
    nodes = [
        node("evidence:e1", "input_anchor", 0, {"anchor_type": "caption", "evidence_id": "e1", "segment_id": "s1", "revision": 1}),
        node("context:c1", "context", 1, {"snapshot_id": "c1", "meeting_state_version": 1, "relevant_context_hash": "a" * 64, "evidence_count": 1}),
        node("execution:root", "execution", 2, {"execution_id": "root", "root_execution_id": "root", "parent_execution_id": None, "profile": "fast_turn" if route == "fast" else "fast_turn", "status": "completed" if route == "fast" else "handed_off", "state_version": 5 if route == "fast" else 4}, 5 if route == "fast" else 4),
    ]
    edges = [TraceEdge(source="execution:root", target="context:c1", kind="derived_from"),
        TraceEdge(source="context:c1", target="evidence:e1", kind="derived_from")]
    fast_statuses = ["received", "contextualizing", "deciding", "responding", "completed"] if route == "fast" else ["received", "contextualizing", "deciding", "handed_off"]
    for version, status in enumerate(fast_statuses, 1):
        event_id = f"event:root:{version}"
        nodes.append(node(event_id, "execution_event", 2 + version,
            {"execution_id": "root", "event_type": f"fast.{status}", "status": status}, version))
        edges.append(TraceEdge(source=event_id, target="execution:root", kind="derived_from"))
    if route == "fast":
        nodes.append(node("model:root", "model_stage", 8, {"execution_id": "root", "sequence": 1, "stage_kind": "plan"}))
        nodes.append(node("terminal:root", "terminal", 9, {"execution_id": "root", "status": "completed", "result_hash": "b" * 64}, 5))
        edges += [TraceEdge(source="model:root", target="execution:root", kind="derived_from"),
            TraceEdge(source="execution:root", target="terminal:root", kind="terminated_as"),
            TraceEdge(source="terminal:root", target="model:root", kind="derived_from")]
    else:
        nodes += [
            node("execution:slow", "execution", 7, {"execution_id": "slow", "root_execution_id": "root", "parent_execution_id": "root", "profile": "action_run", "status": "completed", "state_version": 5, "grant_id": "g1"}, 5),
            node("handoff:h1", "handoff", 7, {"handoff_id": "h1", "source_execution_id": "root", "target_execution_id": "slow", "snapshot_id": "c1", "grant_id": "g1"}),
            node("grant:g1", "grant", 7, {"grant_id": "g1", "status": "consumed", "max_side_effects": 1, "used_side_effects": 1}),
            node("candidate:candidate-1", "input_anchor", 7, {"anchor_type": "candidate", "candidate_id": "candidate-1", "revision": 1}),
        ]
        edges += [
            TraceEdge(source="execution:root", target="execution:slow", kind="parent_of"),
            TraceEdge(source="execution:root", target="execution:slow", kind="handed_off_to"),
            TraceEdge(source="execution:slow", target="context:c1", kind="derived_from"),
            TraceEdge(source="handoff:h1", target="execution:root", kind="derived_from"),
            TraceEdge(source="grant:g1", target="candidate:candidate-1", kind="authorized_by"),
        ]
        statuses = ["queued", "planning", "executing"]
        if route == "unknown":
            statuses += ["waiting_external", "reconciling", "observing", "completed"]
        else:
            statuses += ["observing", "completed"]
        for version, status in enumerate(statuses, 1):
            event_id = f"event:slow:{version}"
            kind = "recovery" if status == "reconciling" else "execution_event"
            nodes.append(node(event_id, kind, 8 + version,
                {"execution_id": "slow", "event_type": f"action.{status}", "status": status}, version))
            edges.append(TraceEdge(source=event_id, target="execution:slow", kind="derived_from"))
        final_version = len(statuses)
        slow_index = next(index for index, value in enumerate(nodes) if value.node_id == "execution:slow")
        nodes[slow_index] = nodes[slow_index].model_copy(update={"state_version": final_version,
            "data": {**nodes[slow_index].data, "state_version": final_version}})
        nodes += [
            node("tool:t1", "tool", 12, {"tool_call_id": "t1", "execution_id": "slow", "tool_name": "task.create", "tool_version": "1", "effect": "external_write", "status": "succeeded", "arguments_hash": "c" * 64, "logical_action_key": "d" * 64, "idempotency_key": "idem-1", "external_reference": {"identifier": "FAKE-1"}, "attempt_count": 2 if route == "unknown" else 1, "create_attempt_count": 1, "reconcile_attempt_count": 1 if route == "unknown" else 0}),
            node("claim:cl1", "claim", 12, {"claim_id": "cl1", "execution_id": "slow", "tool_call_id": "t1", "status": "succeeded", "logical_action_key": "d" * 64, "arguments_hash": "c" * 64}),
            node("terminal:slow", "terminal", 20, {"execution_id": "slow", "status": "completed", "result_hash": "e" * 64}, final_version),
        ]
        edges += [
            TraceEdge(source="execution:slow", target="tool:t1", kind="invoked"),
            TraceEdge(source="tool:t1", target="execution:slow", kind="derived_from"),
            TraceEdge(source="tool:t1", target="grant:g1", kind="authorized_by"),
            TraceEdge(source="tool:t1", target="claim:cl1", kind="claimed_by"),
            TraceEdge(source="claim:cl1", target="execution:slow", kind="derived_from"),
            TraceEdge(source="execution:slow", target="terminal:slow", kind="terminated_as"),
            TraceEdge(source="terminal:slow", target="tool:t1", kind="derived_from"),
        ]
        if route == "unknown":
            edges.append(TraceEdge(source="claim:cl1", target="tool:t1", kind="reconciled_to"))
    return AgentTrace(trace_id="root", root_execution_id="root", session_id="session-1",
        media_session_id="media-1", generated_at=NOW, nodes=tuple(nodes), edges=tuple(edges))


@pytest.mark.parametrize("route", ["fast", "handoff", "unknown"])
def test_three_golden_routes_have_terminal_to_evidence_lineage(route):
    report = validate_trace(golden_trace(route))
    assert report.valid, report.errors


@pytest.mark.parametrize("mutation,error_code", [
    ("parent", "parent_missing"),
    ("version", "state_version_gap"),
    ("grant", "grant_edge_missing"),
    ("evidence", "evidence_path_missing"),
    ("terminal", "terminal_count_invalid"),
    ("redaction", "sensitive_field"),
])
def test_validator_rejects_six_stable_corruptions(mutation, error_code):
    payload = golden_trace("handoff").model_dump(mode="json")
    if mutation == "parent":
        slow = next(node for node in payload["nodes"] if node["node_id"] == "execution:slow")
        slow["data"]["parent_execution_id"] = "missing"
    elif mutation == "version":
        payload["nodes"] = [node for node in payload["nodes"] if node["node_id"] != "event:slow:3"]
        payload["edges"] = [edge for edge in payload["edges"] if edge["source"] != "event:slow:3"]
    elif mutation == "grant":
        payload["edges"] = [edge for edge in payload["edges"] if not (edge["source"] == "tool:t1" and edge["kind"] == "authorized_by")]
    elif mutation == "evidence":
        payload["edges"] = [edge for edge in payload["edges"] if edge["target"] != "evidence:e1"]
    elif mutation == "terminal":
        duplicate = copy.deepcopy(next(node for node in payload["nodes"] if node["node_id"] == "terminal:slow"))
        duplicate["node_id"] = "terminal:duplicate"
        payload["nodes"].append(duplicate)
        payload["edges"].append({"source": "execution:slow", "target": "terminal:duplicate", "kind": "terminated_as"})
    else:
        next(node for node in payload["nodes"] if node["node_id"] == "tool:t1")["data"]["prompt"] = "do not store me"
    codes = {error.error_code for error in validate_trace(payload).errors}
    assert error_code in codes
