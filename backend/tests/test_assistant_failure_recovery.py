import asyncio

import pytest
from sqlalchemy import func, select

from app.assistant.action_runner import ActionRunResult
from app.assistant.action_scheduler import ActionRunScheduler
from app.assistant.context import ContextBuilder
from app.assistant.fast_runner import FastTurnRequest, FastTurnRunner
from app.assistant.repository import AssistantRepository
from app.persistence.models import (
    AssistantEventRecord,
    AssistantHandoffRecord,
    AssistantToolCallRecord,
)


def test_contextualizing_failure_has_no_partial_snapshot_grant_tool_or_handoff(meeting_history, monkeypatch):
    async def fail_context(*args, **kwargs):
        raise RuntimeError("injected context failure")

    monkeypatch.setattr(ContextBuilder, "build", fail_context)
    f = meeting_history
    result = asyncio.run(FastTurnRunner(f.database, object(), object()).run(FastTurnRequest(
        session_id=f.session_id,
        goal="Explain the release",
        client_request_id="context-failure",
    )))
    assert result.status == "failed" and result.error_code == "fast_turn_failed"
    with f.database.session() as db:
        repo = AssistantRepository(db)
        execution = repo.get_execution_required(result.execution_id)
        assert execution.snapshot_id is None and execution.grant_id is None
        assert repo.list_tool_calls(execution.id) == []
        assert db.scalar(select(func.count(AssistantHandoffRecord.id)).where(
            AssistantHandoffRecord.source_execution_id == execution.id,
        )) == 0
        event_types = [event.event_type for event in db.scalars(select(AssistantEventRecord).where(
            AssistantEventRecord.execution_id == execution.id,
        ))]
        assert event_types == ["execution.created", "fast_turn.contextualizing", "fast_turn.failed"]


class ControlledRunner:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, execution_id):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return ActionRunResult(execution_id=execution_id, status="completed")


def test_duplicate_enqueue_and_active_wakeup_dispatch_one_action_run():
    async def scenario():
        runner = ControlledRunner()
        scheduler = ActionRunScheduler(runner, concurrency=2, resume_delay_seconds=0.01)
        await scheduler.start()
        try:
            await asyncio.gather(scheduler.enqueue("execution-one"), scheduler.enqueue("execution-one"))
            await asyncio.wait_for(runner.entered.wait(), 1)
            await scheduler.enqueue("execution-one")
            runner.release.set()
            result = await scheduler.wait("execution-one", timeout=1)
            assert result.status == "completed"
            assert runner.calls == 1
        finally:
            await scheduler.stop()

    asyncio.run(scenario())


def test_action_terminal_state_rejects_scheduler_style_reentry(meeting_history):
    f = meeting_history
    with f.database.session() as db:
        repo = AssistantRepository(db)
        execution = repo.create_execution(
            session_id=f.session_id,
            profile="action_run",
            goal="Terminal execution",
            client_request_id="terminal-action",
        )
        execution = repo.transition_execution(execution.id,
            expected_version=execution.state_version, target_status="planning",
            event_type="test.planning", summary="planning")
        execution = repo.transition_execution(execution.id,
            expected_version=execution.state_version, target_status="failed",
            event_type="test.failed", summary="failed", error_code="injected")
        before = repo.current_event_cursor(f.session_id)
        with pytest.raises(ValueError):
            repo.transition_execution(execution.id,
                expected_version=execution.state_version, target_status="planning",
                event_type="test.reentered", summary="must fail")
        assert repo.current_event_cursor(f.session_id) == before
        assert repo.list_tool_calls(execution.id) == []
