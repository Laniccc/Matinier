from __future__ import annotations

import pytest

from app.sessions.state import (
    InvalidSessionTransition,
    SessionStatus,
    transition_session_status,
)


def test_normal_session_state_chain_is_strict_and_idempotent() -> None:
    chain: tuple[SessionStatus, ...] = (
        "created",
        "starting",
        "running",
        "finalizing",
        "completed",
    )

    current = chain[0]
    assert transition_session_status(current, current) == current
    for target in chain[1:]:
        current = transition_session_status(current, target)
        assert current == target
        assert transition_session_status(current, current) == current


@pytest.mark.parametrize(
    "current",
    [
        "created",
        "starting",
        "running",
        "finalizing",
    ],
)
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_non_terminal_session_can_fail_or_cancel(
    current: SessionStatus,
    terminal: SessionStatus,
) -> None:
    assert transition_session_status(current, terminal) == terminal


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_terminal_session_cannot_change(terminal: SessionStatus) -> None:
    assert transition_session_status(terminal, terminal) == terminal
    with pytest.raises(InvalidSessionTransition):
        transition_session_status(terminal, "created")


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("created", "running"),
        ("starting", "finalizing"),
        ("running", "starting"),
        ("running", "completed"),
        ("finalizing", "running"),
    ],
)
def test_normal_session_transition_cannot_skip_or_regress(
    current: SessionStatus,
    target: SessionStatus,
) -> None:
    with pytest.raises(InvalidSessionTransition):
        transition_session_status(current, target)
