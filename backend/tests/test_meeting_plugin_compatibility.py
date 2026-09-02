from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api.assistant import AssistantCancelRequest, AssistantInputRequest, _execution_summary, cancel_assistant_execution, submit_assistant_input
from app.api.meeting_state import _mark_response
from app.assistant.context import ContextBuilder
from app.assistant.repository import AssistantRepository
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.models import ActionCandidateRecord, ExternalActionClaimRecord
from app.task_system.models import TaskDraft
from meeting_plugin_fakes import ControlledLinear, ControlledMeetingModel, final_segment
from app.api.meeting_state import _projection_segment
from app.meeting_state.models import MeetingState


def test_existing_history_ids_evidence_and_execution_chain(meeting_history):
    f = meeting_history
    with f.database.session() as db:
        repo = AssistantRepository(db)
        child = _execution_summary(repo, repo.get_execution_required(f.child_id))
        assert (child.root_execution_id, child.parent_execution_id) == (f.root_id, f.root_id)
        assert child.needs_input.question == "Which release?"
        assert child.state_version == 3
        assert repo.get_execution_required(f.completed_id).result_json["external_reference"]["identifier"] == "FAKE-1"
        assert db.get(ExternalActionClaimRecord, "old-unknown-claim").status == "unknown"
        marks = [_mark_response(m) for m in MeetingStateRepository(db).list_marks(f.session_id)]
        assert {m.mark_id for m in marks} == {"manual-mark", "automatic-mark"}
        assert all(m.evidence[0].revision == 2 and m.audio_start_ms == 1000 for m in marks)
        assert any(e.message_kind == "user_input" for m in marks for e in m.evidence_messages)
        assert MeetingStateRepository(db).list_marks(f.other_session_id) == []
        assert db.get(ActionCandidateRecord, f.candidate_id).current_revision == 1


def test_existing_input_cancel_replay_and_state_version(meeting_history):
    f = meeting_history
    calls = []
    async def resume(execution_id):
        calls.append(execution_id)
    runtime = SimpleNamespace(action_runtime=SimpleNamespace(scheduler=SimpleNamespace(resume=resume)))
    with f.database.session() as db:
        payload = AssistantInputRequest(client_operation_id="input-1", expected_state_version=3, input="Release one")
        response = asyncio.run(submit_assistant_input(f.child_id, payload, runtime=runtime, db_session=db))
        assert response.execution.status == "planning"
        assert response.execution.state_version == 4
        cancel = AssistantCancelRequest(client_operation_id="cancel-1", expected_state_version=4)
        first = cancel_assistant_execution(f.child_id, cancel, _=runtime, db_session=db)
        replay = cancel_assistant_execution(f.child_id, cancel, _=runtime, db_session=db)
        assert first == replay
        assert first.execution.status == "cancelled"
        assert first.execution.state_version == 5
        assert calls == [f.child_id]
        events = AssistantRepository(db).list_session_events(f.session_id, after=0, limit=100)
        assert len({e.id for e in events}) == len(events)


def test_existing_context_keeps_caption_revision_and_manual_evidence(meeting_history):
    async def scenario():
        with meeting_history.database.session() as db:
            snapshot = await ContextBuilder(db).build(session_id=meeting_history.session_id, goal="Release notes?", persist=False)
            captions = [m for m in snapshot.evidence_messages if m.message_kind == "caption"]
            assert captions and all(m.session_id == meeting_history.session_id for m in captions)
            assert captions[0].segment_revision == 2
    asyncio.run(scenario())


def test_fakes_support_model_gates_and_lost_linear_response():
    async def scenario():
        model = ControlledMeetingModel(blocked=True)
        task = asyncio.create_task(model.extract(state=MeetingState(session_id="meeting-a", version=0), segments=(_projection_segment(final_segment()),)))
        await asyncio.wait_for(model.entered.wait(), 1)
        assert not task.done()
        model.release.set()
        await task
        linear = ControlledLinear(lose_response=True)
        try:
            await linear.create(TaskDraft(title="Notes", description="Notes", source_evidence_ids=("fixture-evidence",)), action_key="a" * 64)
        except TimeoutError:
            pass
        else:
            raise AssertionError("fake must drop the response")
        result = await linear.reconcile_create(action_key="a" * 64)
        assert result.status == "single"
        await linear.get(result.matches[0].external_id)
        assert (linear.create_calls, linear.read_calls, linear.reconcile_calls, linear.task_count) == (1, 1, 1, 1)
    asyncio.run(scenario())
