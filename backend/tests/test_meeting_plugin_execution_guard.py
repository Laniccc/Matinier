import pytest
import asyncio
from types import SimpleNamespace
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.assistant.plugin_execution_guard import MeetingExecutionGuard
from app.assistant.plugin_operations import MeetingPluginOperations
from app.assistant.plugin_repository import MeetingPluginDenied
from app.assistant.plugin_policy import MeetingPluginPolicy
from test_meeting_plugin_operations import authorized_command, admit
from app.assistant.tools import ToolExecutor, ToolRegistry
from app.assistant.tools.contracts import ToolSpec, ToolInvocation, ToolResult
from app.assistant.repository import AssistantRepository
from app.persistence.models import AssistantExecutionRecord, AssistantToolCallRecord, PluginCapabilityGrantRecord, ActionGrantRecord


class FakeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


class FakeLinearTool:
    provider_name = "fake-linear"

    def __init__(self, *, blocked=False, lost=False):
        self.creates, self.reads = 0, 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.lost = lost
        if not blocked:
            self.release.set()

    async def execute(self, context):
        self.creates += 1
        self.entered.set()
        await self.release.wait()
        if self.lost:
            return ToolResult(status="unknown", error_code="response_lost", error_message="Fake response lost")
        return ToolResult(status="succeeded", output={"title": "FAKE-1"}, confirmed_side_effects=1)

    async def reconcile(self, context):
        self.reads += 1
        return ToolResult(status="succeeded", output={"title": "FAKE-1"}, confirmed_side_effects=1)


def write_fixture(f, *, blocked=False, lost=False):
    actions, command = authorized_command(f, action="meeting.execute")
    operations = MeetingPluginOperations(actions)
    guard = MeetingExecutionGuard(operations)
    with f.database.session() as db:
        operation = admit(operations, db, f, command, action="meeting.execute")
        execution_id = operation.execution_id
        db.commit()
    registry = ToolRegistry()
    adapter = FakeLinearTool(blocked=blocked, lost=lost)
    registry.register(ToolSpec(name="fake.create", version="1", capability="task.create", effect="external_write",
        input_model=FakeInput, output_model=FakeInput, timeout_seconds=5,
        supports_idempotency=True, supports_reconciliation=True), adapter)
    executor = ToolExecutor(f.database, registry, execution_guard=guard)
    request = ToolInvocation(execution_id=execution_id, tool_name="fake.create", arguments={"title": "Release notes"},
        candidate_id=f.candidate_id, requested_resource_scope={"linear_team_id": "team-1"},
        idempotency_key="write-1", logical_action_key="release-notes")
    return operations, guard, registry, adapter, executor, request


def test_revoke_blocks_execution_but_stopping_analysis_does_not(meeting_history):
    f = meeting_history
    actions, command = authorized_command(f, action="meeting.execute")
    service = MeetingPluginOperations(actions)
    guard = MeetingExecutionGuard(service)
    with f.database.session() as db:
        execution_id = admit(service, db, f, command, action="meeting.execute").execution_id
        db.commit()
    guard.check(execution_id)
    MeetingPluginPolicy(f.database).deactivate(f.session_id)
    guard.check(execution_id)
    MeetingPluginPolicy(f.database).revoke(f.session_id)
    with pytest.raises(MeetingPluginDenied):
        guard.check(execution_id)


def test_legacy_execution_has_no_implicit_write_authority(meeting_history):
    f = meeting_history
    actions, _ = authorized_command(f)
    with pytest.raises(MeetingPluginDenied):
        MeetingExecutionGuard(MeetingPluginOperations(actions)).check(f.child_id)


@pytest.mark.parametrize("change", ["revoked", "scope_grant", "budget", "team", "candidate", "replacement_grant"])
def test_final_tool_gate_blocks_unapproved_writes(meeting_history, change):
    async def scenario():
        f = meeting_history
        service, guard, registry, adapter, executor, request = write_fixture(f)
        if change == "revoked":
            MeetingPluginPolicy(f.database).revoke(f.session_id)
        elif change == "team":
            service.settings.linear_team_id = "other-team"
        elif change == "replacement_grant":
            request = request.model_copy(update={"grant_id": "forged-grant"})
        else:
            with f.database.session() as db:
                if change == "scope_grant":
                    db.scalar(select(PluginCapabilityGrantRecord)).scope_json = {}
                elif change == "budget":
                    db.scalar(select(ActionGrantRecord)).used_side_effects = 1
                else:
                    from app.persistence.models import SegmentRecord
                    row = db.scalar(select(SegmentRecord).where(SegmentRecord.session_id == f.session_id))
                    row.revision += 1
                db.commit()
        from app.assistant.grants import GrantAuthorizationError
        with pytest.raises((MeetingPluginDenied, GrantAuthorizationError)):
            await executor.execute(request)
        assert adapter.creates == 0
    asyncio.run(scenario())


def test_requesting_is_the_disable_linearization_point(meeting_history):
    async def scenario():
        f = meeting_history
        service, guard, registry, adapter, executor, request = write_fixture(f, blocked=True)
        running = asyncio.create_task(executor.execute(request))
        await asyncio.wait_for(adapter.entered.wait(), 2)
        with f.database.session() as db:
            assert db.scalar(select(AssistantToolCallRecord)).status == "requesting"
        MeetingPluginPolicy(f.database).revoke(f.session_id)
        # Caption writer is free while the issued remote request is blocked.
        from meeting_plugin_fakes import final_segment
        with f.database.session() as db:
            db.add(final_segment(segment_id="during-write"))
            db.commit()
        with pytest.raises(MeetingPluginDenied):
            await executor.execute(request.model_copy(update={"idempotency_key": "write-2", "logical_action_key": "new-write"}))
        adapter.release.set()
        assert (await running).status == "succeeded"
        assert adapter.creates == 1
    asyncio.run(scenario())


def test_unknown_after_revoke_only_reconciles_without_planner_or_duplicate(meeting_history):
    async def scenario():
        f = meeting_history
        service, guard, registry, adapter, executor, request = write_fixture(f, lost=True)
        assert (await executor.execute(request)).status == "unknown"
        MeetingPluginPolicy(f.database).revoke(f.session_id)
        from app.assistant.recovery import ActionRunRecovery
        class Scheduler:
            async def enqueue(self, _):
                raise AssertionError("revoked/legacy recovery must not enqueue a planner")
        report = await ActionRunRecovery(f.database, registry, Scheduler(),
            execution_guard=guard, tool_executor=executor).recover_startup()
        assert report.enqueued_execution_ids == ()
        assert adapter.creates == 1 and adapter.reads == 1
        with f.database.session() as db:
            assert db.get(AssistantExecutionRecord, request.execution_id).status == "needs_input"
            assert db.scalar(select(AssistantToolCallRecord)).status == "succeeded"
    asyncio.run(scenario())


def test_legacy_active_startup_does_not_run_planner(meeting_history):
    async def scenario():
        f = meeting_history
        actions, _ = authorized_command(f)
        guard = MeetingExecutionGuard(MeetingPluginOperations(actions))
        with f.database.session() as db:
            execution = AssistantRepository(db).create_execution(session_id=f.session_id, profile="action_run", goal="Legacy")
            identifier = execution.id
            db.commit()
        from app.assistant.recovery import ActionRunRecovery
        class Scheduler:
            async def enqueue(self, _):
                raise AssertionError("legacy execution must stay read-only")
        await ActionRunRecovery(f.database, ToolRegistry(), Scheduler(), execution_guard=guard).recover_startup()
        with f.database.session() as db:
            assert db.get(AssistantExecutionRecord, identifier).status == "needs_input"
    asyncio.run(scenario())


def test_disable_revokes_before_waiting_for_container_exit(meeting_history, tmp_path):
    async def scenario():
        from app.plugins.bootstrap import PluginHostRuntime
        from app.plugins.container_runtime import PluginIdentity
        f = meeting_history
        service, guard, registry, adapter, executor, request = write_fixture(f)
        service.settings.data_dir = tmp_path
        runtime = PluginHostRuntime(service.settings, f.database)
        entered, release = asyncio.Event(), asyncio.Event()
        class Supervisor:
            def statuses(self):
                return (PluginIdentity("com.matinier.meeting-assistant", "1.0.0"),)
            async def disable(self, _):
                entered.set()
                await release.wait()
        runtime.supervisor = Supervisor()
        runtime.detail = lambda _: {"status": "disabled"}
        pending = asyncio.create_task(runtime.disable_plugin("com.matinier.meeting-assistant"))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with pytest.raises(MeetingPluginDenied):
                await executor.execute(request)
            with f.database.session() as db:
                assert db.scalar(select(PluginCapabilityGrantRecord)).status == "revoked"
            assert adapter.creates == 0
        finally:
            release.set()
            await pending
    asyncio.run(scenario())


@pytest.mark.parametrize("revoke_during_model", [False, True])
def test_no_linear_still_runs_real_fast_runner_and_discards_revoked_answer(meeting_history, revoke_during_model):
    async def scenario():
        import json
        from app.assistant.bootstrap import build_assistant_runtime
        from app.text_processing.provider import StructuredCompletionResult
        from test_host_action_intents import make_actions, prepare, confirm_input
        from app.persistence.models import MeetingPluginOperationRecord
        f = meeting_history
        actions, context = make_actions(f, task_system_provider="disabled", linear_team_id=None)
        with pytest.raises(MeetingPluginDenied, match="task system"):
            prepare(actions, context, f)
        preview = prepare(actions, context, f, action="meeting.ask")
        command = actions.confirm(context, f.media_id, confirm_input(preview))["command"]
        with f.database.session() as db:
            operation = admit(MeetingPluginOperations(actions), db, f, command)
            identifier, execution_id = operation.id, operation.execution_id
            db.commit()
        class Provider:
            provider_name, model = "fake", "test"
            calls = 0
            def __init__(self):
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def complete_structured(self, request):
                self.calls += 1
                self.entered.set()
                await self.release.wait()
                refs = request.input_payload["available_evidence_refs"]
                return StructuredCompletionResult(content=json.dumps({"kind": "respond", "decision_summary": "Answer",
                    "claims": [{"text": "Release notes are required", "evidence_refs": [refs[0]]}]}), finish_reason="stop")
        provider = Provider()
        runtime = build_assistant_runtime(actions.settings, f.database, provider=provider)
        assert not runtime.registry.list_specs()
        await runtime.start()
        try:
            await asyncio.wait_for(provider.entered.wait(), 3)
            if revoke_during_model:
                MeetingPluginPolicy(f.database).revoke(f.session_id)
            provider.release.set()
            await runtime.operation_worker.flush(timeout=3)
            with f.database.session() as db:
                operation = db.get(MeetingPluginOperationRecord, identifier)
                execution = db.get(AssistantExecutionRecord, execution_id)
                assert operation.status == ("failed" if revoke_during_model else "completed")
                if revoke_during_model:
                    assert "response_text" not in (execution.result_json or {})
                else:
                    assert execution.result_json["response_text"] == "Release notes are required"
            assert provider.calls == 1
        finally:
            provider.release.set()
            await runtime.stop()
    asyncio.run(scenario())


def test_scheduler_shutdown_cancels_wait_without_repeating_work():
    async def scenario():
        from app.assistant.action_scheduler import ActionRunScheduler
        entered, release = asyncio.Event(), asyncio.Event()
        class Runner:
            calls = 0
            async def run(self, _):
                self.calls += 1
                entered.set()
                await release.wait()
        runner = Runner()
        scheduler = ActionRunScheduler(runner)
        await scheduler.start()
        await scheduler.enqueue("durable-operation")
        await entered.wait()
        await asyncio.wait_for(scheduler.stop(grace_seconds=0.01), 1)
        assert runner.calls == 1 and not scheduler.started and scheduler.active_count == 0
    asyncio.run(scenario())
