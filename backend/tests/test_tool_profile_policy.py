import asyncio

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.assistant.fast_runner import FastTurnRunner
from app.assistant.repository import AssistantRepository
from app.assistant.tools import (
    StaticToolProvider,
    ToolExecutor,
    ToolInvocation,
    ToolRegistration,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.persistence.models import AssistantToolCallRecord
from app.task_system import FakeTaskSystemAdapter, TaskSystemService, TaskSystemToolProvider


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


class CountingAdapter:
    provider_name = "counting"

    def __init__(self):
        self.calls = 0

    async def execute(self, context):
        self.calls += 1
        return ToolResult(status="succeeded", output={"ok": True})

    async def reconcile(self, context):
        self.calls += 1
        return ToolResult(status="succeeded", output={"ok": True})


def spec(name, *, effect="read", profiles=frozenset()):
    return ToolSpec(
        name=name,
        version="1",
        capability=name,
        effect=effect,
        input_model=EmptyInput,
        output_model=EmptyOutput,
        timeout_seconds=1,
        supports_idempotency=effect == "external_write",
        supports_reconciliation=effect == "external_write",
        allowed_profiles=profiles,
    )


def test_provider_boundary_registers_real_task_tools_and_clamps_external_profiles(meeting_history):
    service = TaskSystemService(meeting_history.database, FakeTaskSystemAdapter())
    registry = ToolRegistry()
    registered = registry.register_provider(TaskSystemToolProvider(service))
    assert {item.spec.name for item in registered} == {"task.search", "task.get", "task.create"}
    assert registry.get("task.create").spec.allowed_profiles == frozenset({"action_run"})
    assert registry.get("task.search").spec.allowed_profiles == frozenset({"fast_turn", "action_run"})

    adapter = CountingAdapter()
    registry = ToolRegistry()
    registry.register_provider(StaticToolProvider("future-provider", (
        ToolRegistration(spec("future.write", effect="external_write",
            profiles=frozenset({"fast_turn", "action_run"})), adapter),
    )))
    assert registry.get("future.write").spec.allowed_profiles == frozenset({"action_run"})


def test_fast_runner_filters_action_only_tools_and_executor_rechecks_real_profile(meeting_history):
    f = meeting_history
    adapter = CountingAdapter()
    registry = ToolRegistry()
    registry.register(spec("action.read", profiles=frozenset({"action_run"})), adapter)
    runner = FastTurnRunner.__new__(FastTurnRunner)
    runner._registry = registry
    assert runner._available_tools() == ()

    with f.database.session() as db:
        execution = AssistantRepository(db).create_execution(
            session_id=f.session_id,
            profile="fast_turn",
            goal="Try an action-only read",
            client_request_id="profile-denied",
        )
        execution_id = execution.id
        db.commit()
    result = asyncio.run(ToolExecutor(f.database, registry).execute(ToolInvocation(
        execution_id=execution_id,
        tool_name="action.read",
        arguments={},
    )))
    assert result.status == "failed" and result.error_code == "tool_profile_denied"
    assert adapter.calls == 0
    with f.database.session() as db:
        assert db.scalar(select(func.count(AssistantToolCallRecord.id)).where(
            AssistantToolCallRecord.execution_id == execution_id,
        )) == 0


def test_external_write_defaults_to_action_run_only():
    assert spec("default.write", effect="external_write").allowed_profiles == frozenset({"action_run"})
