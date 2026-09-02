import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.assistant.context import ContextBuilder
from app.assistant.plugin_policy import MeetingPluginPolicy
from app.assistant.plugin_repository import MeetingPluginDenied
from app.meeting_state.projector import CatchUpTarget, MeetingStateProjector, ProjectorConfig
from app.persistence.models import MeetingPluginSessionRecord, MeetingProjectionOffsetRecord, MeetingStateHeadRecord, SegmentRecord, SessionRecord
from meeting_plugin_fakes import ControlledMeetingModel, final_segment, install_meeting_scope


def make_projector(f, *, blocked=False, chars=12000):
    model = ControlledMeetingModel(blocked=blocked)
    policy = MeetingPluginPolicy(f.database)
    projector = MeetingStateProjector(f.database, model, config=ProjectorConfig(batch_segments=1, max_input_chars=chars), policy=policy)
    return projector, model, policy


async def scan_and_project(projector, session_id):
    segments, _ = projector._scan_updates()
    await projector._buffer_segments(segments)
    await projector._project_one_batch(session_id)


def test_unactivated_scans_do_not_call_model_or_advance_invalid_offsets(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, _ = make_projector(f)
        with f.database.session() as db:
            db.add(final_segment(segment_id="invalid", text=""))
            db.commit()
        await scan_and_project(projector, f.session_id)
        assert model.calls == []
        with f.database.session() as db:
            assert db.scalar(select(func.count(MeetingProjectionOffsetRecord.id))) == 0
    asyncio.run(scenario())


def test_activation_backfills_before_scan_cursor_and_handles_revisions(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f)
        projector._scan_cursor = dt.datetime.now(dt.UTC)
        assert projector._scan_updates()[0] == []
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        await scan_and_project(projector, f.session_id)
        assert model.calls == [((f.session_id, "final-1", 2),)]
        with f.database.session() as db:
            row = db.scalar(select(SegmentRecord).where(SegmentRecord.session_id == f.session_id))
            row.revision = 3
            row.raw_text = row.display_text = "Corrected release notes"
            row.updated_at = dt.datetime.now(dt.UTC)
            db.commit()
        await scan_and_project(projector, f.session_id)
        await scan_and_project(projector, f.session_id)
        assert len(model.calls) == 2
        with f.database.session() as db:
            offsets = list(db.scalars(select(MeetingProjectionOffsetRecord)))
            assert [(o.session_id, o.processed_revision) for o in offsets] == [(f.session_id, 3)]
    asyncio.run(scenario())


def test_disabled_inflight_result_and_error_do_not_write_or_overwrite_reactivation(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f, blocked=True)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        segments, _ = projector._scan_updates()
        await projector._buffer_segments(segments)
        task = asyncio.create_task(projector._project_one_batch(f.session_id))
        await asyncio.wait_for(model.entered.wait(), 1)
        policy.revoke(f.session_id)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        segments, _ = projector._scan_updates()
        await projector._buffer_segments(segments)
        model.release.set()
        await task
        with f.database.session() as db:
            assert db.get(MeetingStateHeadRecord, f.session_id) is None
        await projector._project_one_batch(f.session_id)
        with f.database.session() as db:
            assert db.get(MeetingStateHeadRecord, f.session_id).version == 1
        assert len(model.calls) == 2
    asyncio.run(scenario())


def test_terminal_frontier_drains_only_previously_active_session(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        with f.database.session() as db:
            db.get(SessionRecord, f.session_id).status = "completed"
            db.get(SessionRecord, f.other_session_id).status = "completed"
            db.commit()
        segments, finalizing = projector._scan_updates()
        assert finalizing == {f.session_id}
        with f.database.session() as db:
            db.add(final_segment(segment_id="late-after-end"))
            db.commit()
        await projector._buffer_segments(segments)
        await projector._project_one_batch(f.session_id)
        await scan_and_project(projector, f.session_id)
        assert model.calls == [((f.session_id, "final-1", 2),)]
        with f.database.session() as db:
            assert db.get(MeetingPluginSessionRecord, f.session_id).analysis_state == "completed"
            assert db.get(MeetingStateHeadRecord, f.other_session_id) is None
    asyncio.run(scenario())


def test_fast_ask_history_and_inactive_context_never_requests_catchup(meeting_history):
    async def scenario():
        f = meeting_history
        projector, model, _ = make_projector(f)
        calls = []
        async def request(session_id):
            calls.append(session_id)
            raise AssertionError("inactive context cannot enqueue catch-up")
        projector.request_catch_up = request
        await projector.start()
        try:
            for status in ("active", "completed"):
                with f.database.session() as db:
                    db.get(SessionRecord, f.session_id).status = status
                    db.commit()
                    snapshot = await ContextBuilder(db, projector=projector).build(session_id=f.session_id, goal="Release?", fast_ask=True, persist=False)
                    assert any(e.message_kind == "caption" for e in snapshot.evidence_messages)
            # Yield to any accidentally scheduled task without a timing sleep.
            await asyncio.get_running_loop().run_in_executor(None, lambda: None)
            assert calls == [] and model.calls == []
        finally:
            await projector.stop()
    asyncio.run(scenario())


def test_reactivation_cannot_complete_an_old_epoch_catchup_target(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f)
        first = policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        segments, _ = projector._scan_updates()
        await projector._buffer_segments(segments)
        future = asyncio.get_running_loop().create_future()
        target = CatchUpTarget(session_id=f.session_id, target_revisions={"final-1": 2}, future=future, analysis_epoch=first.analysis_epoch)
        projector._waiters[f.session_id] = [target]
        policy.deactivate(f.session_id)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        await scan_and_project(projector, f.session_id)
        assert len(model.calls) == 1
        assert future.cancelled()
        assert f.session_id not in projector._waiters
    asyncio.run(scenario())


def test_revocation_after_enqueue_prevents_model_call(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        segments, _ = projector._scan_updates()
        await projector._buffer_segments(segments)
        await projector._schedule_if_ready(f.session_id)
        policy.deactivate(f.session_id)
        queued, _ = await asyncio.wait_for(projector._queue.get(), 1)
        await projector._project_one_batch(queued)
        assert model.calls == []
        assert f.session_id not in projector._buffers
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_model", [False, True])
def test_revocation_between_fragments_discards_output_and_failure_metadata(meeting_history, fail_model):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f, blocked=True, chars=8)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        extract = model.extract
        async def controlled_extract(**kwargs):
            result = await extract(**kwargs)
            if fail_model:
                raise TimeoutError("fake model error after revocation")
            return result
        model.extract = controlled_extract
        segments, _ = projector._scan_updates()
        await projector._buffer_segments(segments)
        task = asyncio.create_task(projector._project_one_batch(f.session_id))
        await asyncio.wait_for(model.entered.wait(), 1)
        policy.revoke(f.session_id)
        model.release.set()
        await asyncio.wait_for(task, 1)
        assert len(model.calls) == 1
        with f.database.session() as db:
            assert db.get(MeetingStateHeadRecord, f.session_id) is None
            assert db.scalar(select(func.count(MeetingProjectionOffsetRecord.id))) == 0
    asyncio.run(scenario())


def test_terminal_transition_recovers_discarded_inflight_backlog_before_cursor(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f, blocked=True)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        segments, _ = projector._scan_updates()
        await projector._buffer_segments(segments)
        # Later polls can advance the global cursor while the model is waiting.
        projector._scan_cursor = dt.datetime.now(dt.UTC)
        task = asyncio.create_task(projector._project_one_batch(f.session_id))
        await asyncio.wait_for(model.entered.wait(), 1)
        with f.database.session() as db:
            db.get(SessionRecord, f.session_id).status = "completed"
            db.commit()
        model.release.set()
        await asyncio.wait_for(task, 1)
        assert f.session_id not in projector._buffers
        await scan_and_project(projector, f.session_id)
        with f.database.session() as db:
            assert db.get(MeetingPluginSessionRecord, f.session_id).analysis_state == "completed"
            assert db.get(MeetingStateHeadRecord, f.session_id).version == 1
        assert len(model.calls) == 2
    asyncio.run(scenario())


def test_terminal_frontier_is_checked_before_model_not_only_at_commit(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        segments, _ = projector._scan_updates()
        await projector._buffer_segments(segments)
        with f.database.session() as db:
            db.get(SessionRecord, f.session_id).status = "completed"
            db.scalar(select(SegmentRecord).where(SegmentRecord.session_id == f.session_id)).revision = 3
            db.commit()
        policy.scan_admissions()  # Freeze revision 3, while revision 2 is queued.
        await projector._project_one_batch(f.session_id)
        assert model.calls == []
        await scan_and_project(projector, f.session_id)
        assert model.calls == [((f.session_id, "final-1", 3),)]
    asyncio.run(scenario())


def test_live_workers_require_activation_and_stop_cleans_waiters(meeting_history):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f, blocked=True)
        await projector.start()
        try:
            with pytest.raises(MeetingPluginDenied):
                await projector.request_catch_up(f.session_id)
            assert model.calls == []
            policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
            target = await projector.request_catch_up(f.session_id)
            await asyncio.wait_for(model.entered.wait(), 1)
            assert not target.done
            workers = tuple(projector._workers)
        finally:
            await projector.stop()
        assert target.done and target._future.cancelled()
        assert all(worker.done() for worker in workers)
        assert not projector._buffers and not projector._waiters
        with f.database.session() as db:
            assert db.get(MeetingStateHeadRecord, f.session_id) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_only", [False, True])
def test_empty_or_invalid_terminal_frontier_finishes_without_model(meeting_history, invalid_only):
    async def scenario():
        f = meeting_history
        install_meeting_scope(f.database)
        projector, model, policy = make_projector(f)
        policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
        with f.database.session() as db:
            row = db.scalar(select(SegmentRecord).where(SegmentRecord.session_id == f.session_id))
            row.raw_text = row.display_text = ""
            if not invalid_only:
                row.status = "partial"
            db.get(SessionRecord, f.session_id).status = "completed"
            db.commit()
        await scan_and_project(projector, f.session_id)
        await scan_and_project(projector, f.session_id)
        assert model.calls == []
        with f.database.session() as db:
            assert db.get(MeetingPluginSessionRecord, f.session_id).analysis_state == "completed"
    asyncio.run(scenario())
