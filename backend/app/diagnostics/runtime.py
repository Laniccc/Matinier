from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeSnapshot:
    values: dict[str, Any]
    reported_at: dt.datetime


class RuntimeSnapshotRegistry:
    """Keep a bounded set of safe Worker diagnostics in API process memory."""

    def __init__(self, max_sessions: int = 512) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._max_sessions = max_sessions
        self._snapshots: OrderedDict[str, RuntimeSnapshot] = OrderedDict()

    def update(self, session_id: str, values: dict[str, Any]) -> RuntimeSnapshot:
        if not session_id:
            raise ValueError("session_id is required")
        snapshot = RuntimeSnapshot(
            values=dict(values),
            reported_at=dt.datetime.now(dt.UTC),
        )
        self._snapshots[session_id] = snapshot
        self._snapshots.move_to_end(session_id)
        while len(self._snapshots) > self._max_sessions:
            self._snapshots.popitem(last=False)
        return snapshot

    def get(self, session_id: str) -> RuntimeSnapshot | None:
        snapshot = self._snapshots.get(session_id)
        if snapshot is not None:
            self._snapshots.move_to_end(session_id)
        return snapshot
