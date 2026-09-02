"""No network/DB: deterministic Host transport and controllable poll clock."""
import asyncio
import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugin-sdk/python"))
sys.path.insert(0, str(ROOT / "plugin-sdk/examples/meeting-assistant"))


def state_data(**overrides):
    value = {
        "legacy_session_id": "legacy-1", "media_session_id": "media-1",
        "state": {"session_id": "legacy-1", "version": 0, "highlights": []},
        "state_version": 0, "state_hash": "initial", "snapshot_key": "initial",
        "processing": {"status": "inactive", "analysis_epoch": 0, "authority_epoch": 0,
            "current_finals": 0, "processed_finals": 0, "pending_finals": 0,
            "updated_at": None, "last_error_code": None},
        "freshness": {"status": "stale", "lag_ms": 0},
        "marks": [], "candidates": [], "executions": [], "events": [],
        "event_cursor": 0, "next_cursor": 0, "has_more_events": False,
        "offset": 0, "next_offset": 10, "has_more": False,
    }
    value.update(overrides)
    return value


def execution(status="completed", **changes):
    value = {"execution_id": "exec-1", "session_id": "legacy-1", "root_execution_id": "root-1",
        "parent_execution_id": "root-1", "state_version": 2, "status": status,
        "profile": "fast_turn", "goal": "What changed?", "result": {"answer": "Keep the evidence."},
        "needs_input": None, "external_effects": {"confirmed": 0, "unknown": 0, "existing_actions_remain": False},
        "step_count": 1, "updated_at": "2026-08-31T12:00:00Z", "error_code": None}
    value.update(changes)
    return value


class Clock:
    def __init__(self):
        self.waiting = asyncio.Event()
        self.ticks = asyncio.Queue()

    async def sleep(self, seconds):
        assert seconds == 2
        self.waiting.set()
        await self.ticks.get()
        self.waiting.clear()

    async def tick(self):
        await asyncio.wait_for(self.waiting.wait(), 2)
        self.ticks.put_nowait(None)


class Host:
    def __init__(self):
        self.calls = []
        self.state = state_data()
        self.saved = None
        self.version = 0
        self.views = []
        self.query_error = None
        self.query_entered = None
        self.query_release = None
        self.queried = asyncio.Queue()
        self.operation = {"operation": {"operation_id": "op-1", "status": "running", "execution_id": None, "error_code": None}}
        self.tasks = set()

    def create_task(self, awaitable, *, name):
        task = asyncio.create_task(awaitable, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def capability(self, *, name, session_scope, input_value, idempotency_key=None):
        self.calls.append((name, copy.deepcopy(input_value), idempotency_key, session_scope))
        if name == "state.get":
            return {"value": copy.deepcopy(self.saved), "version": self.version}
        if name == "state.put":
            assert input_value["expected_version"] == self.version
            self.version += 1
            self.saved = copy.deepcopy(input_value["value"])
            return {"version": self.version}
        if name == "ui.publish":
            self.views.append(copy.deepcopy(input_value))
            return {"accepted": True, "view_version": input_value["view_version"]}
        if name == "meeting.state.query":
            if self.query_entered:
                self.query_entered.set()
            if self.query_release:
                await self.query_release.wait()
            if self.query_error:
                raise self.query_error
            self.queried.put_nowait(None)
            return {"data": copy.deepcopy(self.state)}
        if name == "meeting.operation.query":
            return {"data": copy.deepcopy(self.operation)}
        return {"status": "accepted", "operation_id": "op-1"}


def load_plugin():
    spec = importlib.util.spec_from_file_location("meeting_plugin_entry", ROOT / "plugin-sdk/examples/meeting-assistant/plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MeetingAssistantPlugin
