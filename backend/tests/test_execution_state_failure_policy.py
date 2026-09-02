import pytest

from app.assistant.state_machine import (
    ACTION_RUN_TRANSITIONS,
    FAST_TURN_TRANSITIONS,
    is_terminal_execution_status,
    transition_execution_status,
)


@pytest.mark.parametrize("profile,transitions", [
    ("fast_turn", FAST_TURN_TRANSITIONS),
    ("action_run", ACTION_RUN_TRANSITIONS),
])
def test_execution_state_machine_accepts_exact_declared_edges(profile, transitions):
    statuses = set(transitions)
    for current, allowed in transitions.items():
        assert transition_execution_status(profile, current, current) == current
        assert is_terminal_execution_status(profile, current) is (not allowed)
        for target in statuses - {current}:
            if target in allowed:
                assert transition_execution_status(profile, current, target) == target
            else:
                with pytest.raises(ValueError):
                    transition_execution_status(profile, current, target)


def test_execution_state_machine_rejects_foreign_statuses():
    with pytest.raises(ValueError):
        transition_execution_status("fast_turn", "received", "planning")
    with pytest.raises(ValueError):
        transition_execution_status("action_run", "queued", "responding")
