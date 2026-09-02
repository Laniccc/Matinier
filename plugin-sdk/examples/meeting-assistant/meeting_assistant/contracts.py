"""Dependency-free boundary checks; all authorization is still enforced by Host."""
from __future__ import annotations

import copy
import hashlib
import json

PLUGIN_ID = "com.matinier.meeting-assistant"
PLUGIN_VERSION = "1.0.0"
ACTION_CAPABILITIES = {
    "meeting.ask": "meeting.turn.submit", "meeting.execute": "meeting.execution.submit",
    "meeting.mark.create": "meeting.mark.write", "meeting.mark.accept": "meeting.mark.write", "meeting.mark.dismiss": "meeting.mark.write",
    "meeting.input": "meeting.execution.input", "meeting.cancel": "meeting.execution.cancel",
}
# These UI commands only request Host confirmation. They never mint authority.
ACTION_COMMANDS = {**{key.removeprefix("meeting.").replace(".", "_"): key for key in ACTION_CAPABILITIES},
    "analysis_activate": "meeting.analysis.activate", "analysis_deactivate": "meeting.analysis.deactivate"}
COMMANDS = frozenset(ACTION_COMMANDS) | {
    "apply_action", "refresh", "load_more", "previous_page", "detail_next", "detail_previous", "select_candidates", "select_marks", "select_execution",
}
EXECUTION_STATUSES = frozenset({"received", "contextualizing", "deciding", "executing_reads", "responding",
    "handed_off", "completed", "failed", "cancelled", "queued", "planning", "executing", "observing",
    "waiting_external", "reconciling", "needs_input", "partial"})
QUIET_STATUSES = frozenset({"completed", "failed", "cancelled", "handed_off", "partial", "needs_input"})
OPERATION_STATUSES = frozenset({"accepted", "queued", "running", "completed", "failed", "cancelled"})


def identifier(value, maximum=255):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid identifier")
    return value


def integer(value, minimum=0, maximum=1_000_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid integer")
    return value


def ids(value, maximum=50):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("invalid selection")
    result = [identifier(item) for item in value]
    if len(set(result)) != len(result):
        raise ValueError("duplicate selection")
    return result


def object_value(value):
    if not isinstance(value, dict):
        raise ValueError("invalid object")
    return value


def rows(value, maximum=100):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(v, dict) for v in value):
        raise ValueError("invalid rows")
    return value


def data_envelope(response):
    if not isinstance(response, dict) or set(response) != {"data"}:
        raise ValueError("invalid Host envelope")
    data = object_value(response["data"])
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 256 * 1024:
        raise ValueError("Host data exceeds limit")
    return copy.deepcopy(data)


def validate_execution(value, legacy_id):
    row = object_value(value)
    for field in ("execution_id", "root_execution_id"):
        identifier(row.get(field))
    if row.get("session_id") != legacy_id or row.get("status") not in EXECUTION_STATUSES:
        raise ValueError("invalid execution scope/status")
    integer(row.get("state_version"), 1)
    if row.get("parent_execution_id") is not None:
        identifier(row["parent_execution_id"])
    if row.get("result") is not None:
        object_value(row["result"])
    if row.get("needs_input") is not None:
        identifier(object_value(row["needs_input"]).get("question"), 4000)
    effects = object_value(row.get("external_effects"))
    integer(effects.get("confirmed"))
    integer(effects.get("unknown"))
    return row


def state_response(response, media_id):
    data = data_envelope(response)
    if data.get("media_session_id") != media_id:
        raise ValueError("wrong Host media scope")
    legacy = identifier(data.get("legacy_session_id"))
    state = object_value(data.get("state"))
    if state.get("session_id") != legacy:
        raise ValueError("wrong Host legacy scope")
    version = integer(data.get("state_version"))
    if state.get("version") != version:
        raise ValueError("inconsistent state version")
    processing = object_value(data.get("processing"))
    if processing.get("status") not in {"inactive", "active", "draining", "completed"}:
        raise ValueError("invalid analysis state")
    for field in ("analysis_epoch", "authority_epoch", "current_finals", "processed_finals", "pending_finals"):
        integer(processing.get(field))
    object_value(data.get("freshness"))
    for field in ("event_cursor", "next_cursor", "offset", "next_offset"):
        integer(data.get(field))
    for field in ("has_more", "has_more_events"):
        if type(data.get(field)) is not bool:
            raise ValueError("invalid pagination")
    for field in ("marks", "candidates", "executions", "events"):
        for row in rows(data.get(field)):
            if row.get("session_id") != legacy:
                raise ValueError("wrong resource scope")
            if field == "executions":
                validate_execution(row, legacy)
            if field in {"marks", "candidates"}:
                identifier(row.get("mark_id" if field == "marks" else "candidate_id"))
    return data


def operation_response(response, legacy_id, *, operation_id=None, execution_id=None):
    data = data_envelope(response)
    operation = data.get("operation")
    if operation_id is not None:
        operation = object_value(operation)
        if operation.get("operation_id") != operation_id or operation.get("status") not in OPERATION_STATUSES:
            raise ValueError("wrong operation identity/status")
    execution = data.get("execution")
    if execution is not None:
        validate_execution(execution, legacy_id)
        expected = execution_id or (operation or {}).get("execution_id")
        if expected and execution["execution_id"] != expected:
            raise ValueError("wrong execution identity")
        for field in ("steps", "tool_calls", "events"):
            rows(data.get(field, []))
        integer(data.get("next_cursor", 0))
        integer(data.get("event_cursor", 0))
    elif execution_id is not None:
        raise ValueError("missing execution")
    return data


def authorized_command(payload):
    value = object_value(payload)
    if set(value) != {"status", "action", "command"} or value["status"] != "authorized":
        raise ValueError("Host authorization capsule required")
    canonical_action = value["action"]
    if canonical_action not in ACTION_CAPABILITIES:
        raise ValueError("unsupported Host action")
    action = canonical_action.removeprefix("meeting.")
    command = object_value(value["command"])
    request = identifier(command.get("request_id"))
    token = identifier(command.get("intent_token"), 256)
    if len(token) < 32:
        raise ValueError("invalid intent")
    allowed = {"request_id", "intent_token"}
    if action in {"ask", "execute"}:
        allowed |= {"message", "mark_ids"}
        identifier(command.get("message"), 4000)
        ids(command.get("mark_ids", []))
        if action == "execute":
            allowed.add("candidate_ids")
            if not ids(command.get("candidate_ids"), 20):
                raise ValueError("candidates required")
    elif action in {"input", "cancel"}:
        allowed |= {"execution_id", "expected_state_version"}
        identifier(command.get("execution_id"))
        integer(command.get("expected_state_version"), 1)
        if action == "input":
            allowed.add("input")
            identifier(command.get("input"), 4000)
    else:
        allowed |= {"operation", "mark_id", "expected_state_version", "kind", "title", "note", "evidence"}
        operation = action.split(".")[1]
        if command.get("operation") != operation:
            raise ValueError("mark action mismatch")
        evidence = rows(command.get("evidence", []), 50)
        for row in evidence:
            if set(row) != {"segment_id", "revision"}:
                raise ValueError("invalid evidence")
            identifier(row.get("segment_id"))
            integer(row.get("revision"), 1)
        ids([row["segment_id"] for row in evidence])
        if operation == "create":
            identifier(command.get("title"), 500)
            if not evidence or command.get("mark_id") is not None:
                raise ValueError("create needs evidence, not a mark ID")
        else:
            identifier(command.get("mark_id"))
            integer(command.get("expected_state_version"))
            if operation == "accept" and not evidence:
                raise ValueError("accept needs evidence")
        if command.get("kind", "highlight") not in {"highlight", "decision", "action", "conflict"}:
            raise ValueError("invalid mark kind")
        if command.get("note") is not None and (not isinstance(command["note"], str) or len(command["note"]) > 2000):
            raise ValueError("invalid mark note")
    if set(command) - allowed:
        raise ValueError("unknown command fields")
    # Stable across retries; never incorporate/reveal the one-shot secret.
    key = "meeting-" + hashlib.sha256(f"{canonical_action}:{request}".encode()).hexdigest()
    return ACTION_CAPABILITIES[canonical_action], copy.deepcopy(command), key
