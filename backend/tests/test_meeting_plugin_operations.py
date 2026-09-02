import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.assistant.plugin_operations import MeetingPluginOperations
from app.assistant.plugin_runtime import MeetingPluginOperationWorker
from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginConflict, MeetingPluginDenied
from app.assistant.repository import AssistantRepository
from app.persistence.models import (
    ActionGrantRecord,
    AssistantActionIntentRecord,
    AssistantContextSnapshotRecord,
    AssistantEventRecord,
    AssistantExecutionRecord,
    MeetingPluginOperationRecord,
)
from test_host_action_intents import make_actions, prepare, confirm_input


def authorized_command(f, *, action="meeting.ask", **changes):
    actions, context = make_actions(f)
    preview = prepare(actions, context, f, action=action, **changes)
    command = actions.confirm(context, f.media_id, confirm_input(preview))["command"]
    return actions, command


def admit(service, db, f, command, *, action="meeting.ask"):
    return service.admit(db, plugin_id=MEETING_PLUGIN_ID, plugin_version="1.0.0",
        media_session_id=f.media_id, action=action, command=command)


class ControlledFastRunner:
    def __init__(self, database, *, blocked=False):
        self.database = database
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def run(self, request):
        self.calls += 1
        with self.database.session() as db:
            repo = AssistantRepository(db)
            row = repo.get_execution_by_client_request(session_id=request.session_id, client_request_id=request.client_request_id)
            for status in ("contextualizing", "deciding"):
                row = repo.transition_execution(row.id, expected_version=row.state_version, target_status=status, event_type="test.fast", summary=status)
            db.commit()
        self.entered.set()
        await self.release.wait()
        with self.database.session() as db:
            repo = AssistantRepository(db)
            row = repo.get_execution_required(row.id)
            for status in ("responding", "completed"):
                row = repo.transition_execution(row.id, expected_version=row.state_version, target_status=status, event_type="test.fast", summary=status, result={"response_text": "answer"})
            db.commit()
        return SimpleNamespace(execution_id=row.id, status="completed")


def test_admission_is_atomic_idempotent_and_reserves_original_execution(meeting_history):
    f = meeting_history
    actions, command = authorized_command(f)
    service = MeetingPluginOperations(actions)
    with f.database.session() as db:
        first = admit(service, db, f, command)
        again = admit(service, db, f, command)
        assert first.id == again.id and first.execution_id == again.execution_id
        assert db.get(AssistantExecutionRecord, first.execution_id).status == "received"
        with pytest.raises(MeetingPluginConflict):
            admit(service, db, f, {**command, "message": "changed"})
        db.rollback()
    with f.database.session() as db:
        assert db.scalar(select(func.count(MeetingPluginOperationRecord.id))) == 0
        assert db.scalar(select(func.count(AssistantExecutionRecord.id))) == 3
        assert db.scalar(select(AssistantActionIntentRecord)).status == "active"


def test_execute_admission_freezes_context_once_without_copying_source_text(meeting_history):
    f = meeting_history
    actions, command = authorized_command(f, action="meeting.execute")
    service = MeetingPluginOperations(actions)
    with f.database.session() as db:
        first = admit(service, db, f, command, action="meeting.execute")
        replay = admit(service, db, f, command, action="meeting.execute")
        assert replay.id == first.id
        execution_id = first.execution_id
        db.commit()

    with f.database.session() as db:
        replay = admit(service, db, f, command, action="meeting.execute")
        assert replay.execution_id == execution_id
        events = list(db.scalars(select(AssistantEventRecord).where(
            AssistantEventRecord.execution_id == execution_id,
        ).order_by(AssistantEventRecord.id)))
        assert [event.event_type for event in events].count("execution.created") == 1
        frozen = [event for event in events if event.event_type == "action.context_frozen"]
        assert len(frozen) == 1
        execution = db.get(AssistantExecutionRecord, execution_id)
        snapshot = db.get(AssistantContextSnapshotRecord, execution.snapshot_id)
        assert frozen[0].payload_json == {
            "snapshot_id": execution.snapshot_id,
            "meeting_state_version": snapshot.meeting_state_version,
            "evidence_count": len(snapshot.evidence_refs_json),
        }
        assert db.scalar(select(func.count(ActionGrantRecord.id))) == 1


def test_durable_queue_recovers_without_memory_enqueue_and_replay_does_not_rerun(meeting_history):
    async def scenario():
        f = meeting_history
        actions, command = authorized_command(f)
        service = MeetingPluginOperations(actions)
        with f.database.session() as db:
            operation = admit(service, db, f, command)
            operation_id, execution_id = operation.id, operation.execution_id
            db.commit()  # Simulate process exit before any in-memory enqueue.
        runner = ControlledFastRunner(f.database)
        runtime = SimpleNamespace(fast_runner=runner, action_runtime=None)
        worker = MeetingPluginOperationWorker(service, runtime)
        await worker.start()
        await worker.flush(timeout=3)
        await worker.stop()
        worker = MeetingPluginOperationWorker(service, runtime)
        await worker.start()
        await worker.flush(timeout=3)
        await worker.stop()
        with f.database.session() as db:
            assert admit(service, db, f, command).id == operation_id
            assert db.get(MeetingPluginOperationRecord, operation_id).status == "completed"
            assert db.get(AssistantExecutionRecord, execution_id).result_json["response_text"] == "answer"
        assert runner.calls == 1
    asyncio.run(scenario())


def test_slow_model_holds_no_database_transaction_and_crash_does_not_repeat_billing(meeting_history):
    async def scenario():
        f = meeting_history
        actions, command = authorized_command(f)
        service = MeetingPluginOperations(actions)
        with f.database.session() as db:
            operation_id = admit(service, db, f, command).id
            db.commit()
        runner = ControlledFastRunner(f.database, blocked=True)
        runtime = SimpleNamespace(fast_runner=runner, action_runtime=None)
        worker = MeetingPluginOperationWorker(service, runtime)
        await worker.start()
        await asyncio.wait_for(runner.entered.wait(), 2)
        from meeting_plugin_fakes import final_segment
        with f.database.session() as db:
            db.add(final_segment(segment_id="while-model-waits"))
            db.commit()
        await worker.stop()
        await worker.start()
        await worker.flush(timeout=3)
        await worker.stop()
        assert runner.calls == 1
        with f.database.session() as db:
            operation = db.get(MeetingPluginOperationRecord, operation_id)
            assert operation.status == "failed"
            assert operation.error_code == "model_outcome_unknown"
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["missing", "expired", "reused_request", "revoked"])
def test_admission_rechecks_intent_and_authority(meeting_history, change):
    f = meeting_history
    actions, command = authorized_command(f)
    service = MeetingPluginOperations(actions)
    if change == "missing":
        command["intent_token"] = "z" * 48
    elif change == "expired":
        import datetime as dt
        actions.now = lambda: dt.datetime.now(dt.UTC) + dt.timedelta(minutes=16)
    elif change == "reused_request":
        command["request_id"] = "other-request"
    else:
        from app.assistant.plugin_policy import MeetingPluginPolicy
        MeetingPluginPolicy(f.database).revoke(f.session_id)
    with f.database.session() as db:
        with pytest.raises(MeetingPluginDenied):
            admit(service, db, f, command)
