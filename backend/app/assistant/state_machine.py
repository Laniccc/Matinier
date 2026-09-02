from __future__ import annotations

from typing import Literal, cast


ExecutionProfile = Literal["fast_turn", "action_run"]
FastTurnStatus = Literal[
    "received",
    "contextualizing",
    "deciding",
    "executing_reads",
    "responding",
    "handed_off",
    "completed",
    "failed",
    "cancelled",
]
ActionRunStatus = Literal[
    "queued",
    "planning",
    "executing",
    "observing",
    "waiting_external",
    "reconciling",
    "needs_input",
    "completed",
    "partial",
    "failed",
    "cancelled",
]
ExecutionStatus = FastTurnStatus | ActionRunStatus
ExternalActionClaimStatus = Literal[
    "reserved",
    "requesting",
    "unknown",
    "succeeded",
    "failed_safe",
]
ToolCallStatus = Literal[
    "prepared", "requesting", "pending", "succeeded", "failed", "unknown", "reconciling",
]
SubagentStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


FAST_TURN_TRANSITIONS: dict[str, frozenset[str]] = {
    "received": frozenset({"contextualizing", "failed", "cancelled"}),
    "contextualizing": frozenset({"deciding", "failed", "cancelled"}),
    "deciding": frozenset(
        {
            "responding",
            "executing_reads",
            "handed_off",
            "failed",
            "cancelled",
        }
    ),
    "executing_reads": frozenset(
        {"responding", "handed_off", "failed", "cancelled"}
    ),
    "responding": frozenset({"completed", "failed", "cancelled"}),
    "handed_off": frozenset(),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

ACTION_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"planning", "failed", "cancelled"}),
    "planning": frozenset(
        {"queued", "executing", "needs_input", "failed", "cancelled"}
    ),
    "executing": frozenset(
        {
            "observing",
            "waiting_external",
            "reconciling",
            "failed",
            "cancelled",
        }
    ),
    "observing": frozenset(
        {"queued", "planning", "completed", "partial", "failed", "cancelled"}
    ),
    "waiting_external": frozenset(
        {"observing", "reconciling", "failed", "cancelled"}
    ),
    "reconciling": frozenset(
        {"observing", "needs_input", "failed", "cancelled"}
    ),
    "needs_input": frozenset({"planning", "failed", "cancelled"}),
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

EXTERNAL_ACTION_CLAIM_TRANSITIONS: dict[str, frozenset[str]] = {
    "reserved": frozenset({"requesting", "failed_safe"}),
    "requesting": frozenset({"unknown", "succeeded", "failed_safe"}),
    "unknown": frozenset({"succeeded", "failed_safe"}),
    "succeeded": frozenset(),
    "failed_safe": frozenset(),
}

TOOL_CALL_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"requesting", "failed"}),
    "requesting": frozenset({"pending", "succeeded", "failed", "unknown"}),
    "pending": frozenset({"reconciling", "unknown", "failed"}),
    "unknown": frozenset({"reconciling", "failed"}),
    "reconciling": frozenset({"pending", "succeeded", "failed", "unknown"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
}

SUBAGENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"queued", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def initial_execution_status(profile: ExecutionProfile) -> ExecutionStatus:
    return "received" if profile == "fast_turn" else "queued"


def validate_execution_status(
    profile: ExecutionProfile,
    status: str,
) -> ExecutionStatus:
    transitions = (
        FAST_TURN_TRANSITIONS if profile == "fast_turn" else ACTION_RUN_TRANSITIONS
    )
    if status not in transitions:
        raise ValueError(f"invalid {profile} status: {status}")
    return cast(ExecutionStatus, status)


def transition_execution_status(
    profile: ExecutionProfile,
    current: str,
    target: str,
) -> ExecutionStatus:
    current_status = validate_execution_status(profile, current)
    target_status = validate_execution_status(profile, target)
    if current_status == target_status:
        return current_status
    transitions = (
        FAST_TURN_TRANSITIONS if profile == "fast_turn" else ACTION_RUN_TRANSITIONS
    )
    if target_status not in transitions[current_status]:
        raise ValueError(
            f"invalid {profile} transition: {current_status} -> {target_status}"
        )
    return target_status


def is_terminal_execution_status(
    profile: ExecutionProfile,
    status: str,
) -> bool:
    checked = validate_execution_status(profile, status)
    transitions = (
        FAST_TURN_TRANSITIONS if profile == "fast_turn" else ACTION_RUN_TRANSITIONS
    )
    return not transitions[checked]


def transition_external_action_claim(
    current: ExternalActionClaimStatus,
    target: ExternalActionClaimStatus,
) -> ExternalActionClaimStatus:
    if current == target:
        return current
    if target not in EXTERNAL_ACTION_CLAIM_TRANSITIONS[current]:
        raise ValueError(f"invalid external action claim transition: {current} -> {target}")
    return target


def transition_tool_call_status(current: ToolCallStatus, target: ToolCallStatus) -> ToolCallStatus:
    if current not in TOOL_CALL_TRANSITIONS or target not in TOOL_CALL_TRANSITIONS:
        raise ValueError(f"invalid ToolCall status: {current} -> {target}")
    if current == target:
        return current
    if target not in TOOL_CALL_TRANSITIONS[current]:
        raise ValueError(f"invalid ToolCall transition: {current} -> {target}")
    return target


def transition_subagent_status(current: SubagentStatus, target: SubagentStatus) -> SubagentStatus:
    if current not in SUBAGENT_TRANSITIONS or target not in SUBAGENT_TRANSITIONS:
        raise ValueError(f"invalid Subagent status: {current} -> {target}")
    if current == target:
        return current
    if target not in SUBAGENT_TRANSITIONS[current]:
        raise ValueError(f"invalid Subagent transition: {current} -> {target}")
    return target
