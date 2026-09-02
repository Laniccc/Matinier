from __future__ import annotations

from typing import Literal, TypeAlias, cast


SessionStatus: TypeAlias = Literal[
    "created",
    "starting",
    "running",
    "finalizing",
    "completed",
    "failed",
    "cancelled",
]

NORMAL_SESSION_STATES: tuple[SessionStatus, ...] = (
    "created",
    "starting",
    "running",
    "finalizing",
    "completed",
)
TERMINAL_SESSION_STATES: frozenset[SessionStatus] = frozenset(
    {"completed", "failed", "cancelled"}
)
SESSION_STATUSES: frozenset[str] = frozenset(
    {*NORMAL_SESSION_STATES, *TERMINAL_SESSION_STATES}
)


class InvalidSessionTransition(ValueError):
    """Raised when a Session lifecycle transition would skip or regress."""


def parse_session_status(value: str) -> SessionStatus:
    if value not in SESSION_STATUSES:
        raise ValueError(f"unsupported session status: {value}")
    return cast(SessionStatus, value)


def transition_session_status(
    current: SessionStatus,
    target: SessionStatus,
) -> SessionStatus:
    if current == target:
        return current
    if current in TERMINAL_SESSION_STATES:
        raise InvalidSessionTransition(
            f"terminal session cannot transition from {current} to {target}"
        )
    if target in {"failed", "cancelled"}:
        return target
    current_index = NORMAL_SESSION_STATES.index(current)
    if (
        current_index + 1 < len(NORMAL_SESSION_STATES)
        and NORMAL_SESSION_STATES[current_index + 1] == target
    ):
        return target
    raise InvalidSessionTransition(
        f"invalid session transition from {current} to {target}"
    )
