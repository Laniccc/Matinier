import pytest
from sqlalchemy import select

from app.assistant.plugin_contracts import MeetingOperationQueryInput, MeetingStateQueryInput
from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginDenied
from app.assistant.plugin_service import MeetingPluginReadService
from app.persistence.models import AssistantExecutionRecord, MeetingProjectionOffsetRecord, SegmentRecord
from meeting_plugin_fakes import install_meeting_scope


def query(service, f, **changes):
    return service.query(
        media_session_id=f.media_id, plugin_id=MEETING_PLUGIN_ID,
        plugin_version="1.0.0", query=MeetingStateQueryInput(**changes),
    )


def test_read_model_preserves_history_and_current_final_counts(meeting_history):
    f = meeting_history
    install_meeting_scope(f.database)
    with f.database.session() as db:
        service = MeetingPluginReadService(db)
        result = query(service, f)
        assert result["legacy_session_id"] == f.session_id
        assert result["processing"]["status"] == "inactive"
        assert result["processing"]["current_finals"] == 1
        assert result["processing"]["processed_finals"] == 0
        assert result["processing"]["pending_finals"] == 1
        assert {m["mark_id"] for m in result["marks"]} == {"manual-mark", "automatic-mark"}
        assert result["candidates"][0]["candidate_id"] == f.candidate_id
        assert result["marks"][0]["audio_start_ms"] == 1000
        executions = {e["execution_id"]: e for e in result["executions"]}
        assert executions[f.child_id]["parent_execution_id"] == f.root_id
        assert executions[f.child_id]["needs_input"]["question"] == "Which release?"
        assert result["event_cursor"] >= max(e["event_id"] for e in result["events"])
        assert query(service, f)["snapshot_key"] == result["snapshot_key"]
        assert not db.new and not db.dirty


def test_read_pages_and_scope_checks_do_not_disclose_other_sessions(meeting_history):
    f = meeting_history
    install_meeting_scope(f.database)
    install_meeting_scope(f.database, f.other_session_id, f.other_media_id)
    with f.database.session() as db:
        service = MeetingPluginReadService(db)
        first = query(service, f, limit=1)
        second = query(service, f, limit=1, offset=1, after=first["next_cursor"])
        assert first["executions"][0]["execution_id"] != second["executions"][0]["execution_id"]
        assert first["has_more"] and second["events"][0]["event_id"] > first["next_cursor"]
        for params in (
            {"plugin_id": "com.example.other", "plugin_version": "1.0.0"},
            {"plugin_id": MEETING_PLUGIN_ID, "plugin_version": "2.0.0"},
        ):
            with pytest.raises(MeetingPluginDenied):
                service.query(media_session_id=f.media_id, query=MeetingStateQueryInput(), **params)
        with pytest.raises(MeetingPluginDenied):
            service.operation(media_session_id=f.other_media_id, plugin_id=MEETING_PLUGIN_ID,
                plugin_version="1.0.0", query=MeetingOperationQueryInput(execution_id=f.child_id))
        with pytest.raises(MeetingPluginDenied):
            service.require_mark(f.other_session_id, "manual-mark")
        with pytest.raises(MeetingPluginDenied):
            service.require_candidate(f.other_session_id, f.candidate_id)


def test_execution_public_filter_and_revision_counts(meeting_history):
    f = meeting_history
    install_meeting_scope(f.database)
    with f.database.session() as db:
        db.get(AssistantExecutionRecord, f.completed_id).result_json = {
            "answer": "visible", "nested": {"hidden_reasoning": "secret", "raw_error": "secret",
                "intent_token": "secret", "API_KEY": "secret", "ui_nonce": "secret"},
        }
        db.add(MeetingProjectionOffsetRecord(id="read-offset", session_id=f.session_id,
            segment_id="final-1", processed_revision=1))
        db.commit()
        service = MeetingPluginReadService(db)
        result = query(service, f)
        assert result["processing"]["pending_finals"] == 1  # Current revision is 2.
        detail = service.operation(media_session_id=f.media_id, plugin_id=MEETING_PLUGIN_ID,
            plugin_version="1.0.0", query=MeetingOperationQueryInput(execution_id=f.completed_id))
        assert detail["execution"]["result"] == {"answer": "visible", "nested": {}}
