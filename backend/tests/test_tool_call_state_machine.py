import hashlib

import pytest

from app.assistant.repository import AssistantRepository, AssistantStateConflictError
from app.assistant.state_machine import (
    EXTERNAL_ACTION_CLAIM_TRANSITIONS,
    SUBAGENT_TRANSITIONS,
    TOOL_CALL_TRANSITIONS,
    transition_external_action_claim,
    transition_subagent_status,
    transition_tool_call_status,
)


@pytest.mark.parametrize("transitions,transition", [
    (TOOL_CALL_TRANSITIONS, transition_tool_call_status),
    (SUBAGENT_TRANSITIONS, transition_subagent_status),
    (EXTERNAL_ACTION_CLAIM_TRANSITIONS, transition_external_action_claim),
])
def test_persisted_state_machines_accept_only_declared_edges(transitions, transition):
    statuses = set(transitions)
    for current, allowed in transitions.items():
        assert transition(current, current) == current
        for target in statuses - {current}:
            if target in allowed:
                assert transition(current, target) == target
            else:
                with pytest.raises(ValueError):
                    transition(current, target)


def test_tool_call_cas_and_terminal_result_are_immutable(meeting_history):
    f = meeting_history
    digest = hashlib.sha256(b"{}").hexdigest()
    with f.database.session() as db:
        repo = AssistantRepository(db)
        call = repo.prepare_tool_call(
            execution_id=f.child_id,
            tool_name="test.read",
            tool_version="1",
            capability="test.read",
            effect="read",
            arguments={},
            arguments_hash=digest,
        )
        call = repo.update_tool_call(call.id, expected_status="prepared",
            status="requesting", increment_attempt=True)
        with pytest.raises(AssistantStateConflictError):
            repo.update_tool_call(call.id, expected_status="prepared", status="failed")
        call = repo.update_tool_call(call.id, expected_status="requesting", status="succeeded",
            result={"status": "succeeded", "output": {"ok": True}})
        assert call.attempt_count == 1
        assert repo.update_tool_call(call.id, expected_status="succeeded", status="succeeded").id == call.id
        with pytest.raises((ValueError, AssistantStateConflictError)):
            repo.update_tool_call(call.id, expected_status="succeeded", status="requesting")


def test_subagent_recovery_edge_is_explicit_and_terminal_is_immutable(meeting_history):
    f = meeting_history
    with f.database.session() as db:
        repo = AssistantRepository(db)
        branch = repo.create_subagent_run(
            execution_id=f.child_id,
            planning_round=2,
            role="evidence",
            task={"goal": "verify"},
            budget={"max_model_calls": 1},
            snapshot_id=None,
        )
        branch = repo.transition_subagent_run(branch.id, expected_status="queued", status="running")
        branch = repo.transition_subagent_run(branch.id, expected_status="running", status="queued")
        branch = repo.transition_subagent_run(branch.id, expected_status="queued", status="running")
        branch = repo.transition_subagent_run(branch.id, expected_status="running", status="completed",
            result={"ok": True})
        with pytest.raises((ValueError, AssistantStateConflictError)):
            repo.transition_subagent_run(branch.id, expected_status="completed", status="running")
