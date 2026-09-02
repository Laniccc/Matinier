import pytest
from types import SimpleNamespace
from sqlalchemy import func, select

from app.assistant.plugin_contracts import MeetingStateQueryInput
from app.assistant.plugin_history import MeetingPluginHistory
from app.assistant.plugin_history_view import history_document
from app.assistant.plugin_service import _unresolved_task_outcome
from app.persistence.models import ActionGrantRecord, MeetingPluginSessionRecord, PluginInstallationRecord
from meeting_plugin_fakes import install_meeting_scope


UNRESOLVED_ASSIGNEE_NOTICE = (
    "负责人未能映射到 Linear 用户，已把会议中的原称呼保留在 Issue 描述；"
    "请在 Linear 中确认负责人。"
)


def rendered_text(value):
    if isinstance(value, dict):
        return " ".join(rendered_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(rendered_text(item) for item in value)
    return str(value)


@pytest.mark.parametrize("installation", ["absent", "disabled", "removed"])
def test_host_history_is_independent_of_plugin_and_analysis(meeting_history, installation):
    f = meeting_history
    if installation != "absent":
        install_meeting_scope(f.database)
    with f.database.session() as db:
        row = db.scalar(select(PluginInstallationRecord))
        if installation == "disabled":
            row.status = "disabled"
        elif installation == "removed":
            db.delete(row)
        db.commit()
        history = MeetingPluginHistory(db)
        descriptor = history.describe(f.media_id)
        assert descriptor["source_kind"] == "host_history"
        assert descriptor["legacy_session_id"] == f.session_id
        assert descriptor["read_only"] is True
        assert descriptor["controls"] == [{"action": "meeting.cancel", "source": "host_registry"}]
        view = history.query(f.media_id, MeetingStateQueryInput())
        assert {e["execution_id"] for e in view["executions"]} == {f.root_id, f.child_id, f.completed_id}
        assert db.scalar(select(func.count(MeetingPluginSessionRecord.legacy_session_id))) == 0
        assert db.scalar(select(func.count(ActionGrantRecord.id))) == 0
        assert not db.dirty and not db.new


def test_host_history_notice_uses_structured_unresolved_identity():
    unresolved = {"candidates": [{
        "candidate_id": "candidate-1",
        "content": {"assignee": {
            "value": {"spoken_text": "小王", "linear_user_id": None, "is_placeholder": True},
            "resolution": "missing",
        }},
    }]}
    assert UNRESOLVED_ASSIGNEE_NOTICE in rendered_text(history_document(unresolved, 1))

    resolved = {"candidates": [{
        "candidate_id": "candidate-1",
        "content": {"assignee": {
            "value": {"spoken_text": "Alice", "linear_user_id": "linear-user-1", "is_placeholder": False},
            "resolution": "known",
        }},
    }]}
    assert UNRESOLVED_ASSIGNEE_NOTICE not in rendered_text(history_document(resolved, 1))

    outcome = {"tool_calls": [{
        "tool_name": "task.create",
        "outcome": {
            "partial": True,
            "unresolved_identity": {
                "spoken_text": "小王",
                "resolution": "ambiguous",
                "is_placeholder": True,
            },
        },
    }]}
    assert UNRESOLVED_ASSIGNEE_NOTICE in rendered_text(history_document(outcome, 1))


def test_plugin_read_projection_exposes_only_structured_identity_warning_fields():
    record = SimpleNamespace(
        tool_name="task.create",
        result_json={
            "status": "succeeded",
            "error_message": "arbitrary text that must not control the warning",
            "output": {"outcome": {
                "partial": True,
                "unresolved_identity": {
                    "spoken_text": "小王",
                    "external_user_id": None,
                    "resolution": "missing",
                    "source": "provider_search",
                    "is_placeholder": True,
                },
            }},
        },
    )
    assert _unresolved_task_outcome(record) == {
        "partial": True,
        "unresolved_identity": {
            "spoken_text": "小王",
            "resolution": "missing",
            "is_placeholder": True,
        },
    }
    record.result_json["output"]["outcome"]["partial"] = False
    assert _unresolved_task_outcome(record) is None
