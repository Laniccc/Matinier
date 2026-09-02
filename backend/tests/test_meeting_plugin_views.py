import copy
import json

import pytest

from meeting_plugin_client_fakes import ROOT, execution
from meeting_plugin_view_fixtures import cases, documents
from meeting_assistant.contracts import COMMANDS
from meeting_assistant.view import build_meeting_view
from app.plugins.ui_schema import parse_plugin_view


UNRESOLVED_ASSIGNEE_NOTICE = (
    "负责人未能映射到 Linear 用户，已把会议中的原称呼保留在 Issue 描述；"
    "请在 Linear 中确认负责人。"
)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


@pytest.mark.parametrize("case", list(cases()))
def test_every_state_is_closed_schema_and_six_regions(case):
    view = build_meeting_view(**cases()[case])
    parse_plugin_view(view, allowed_commands=COMMANDS)
    assert len([n for n in nodes(view) if n.get("type") == "section" and n.get("id") in {"analysis", "ask", "candidates", "marks", "executions", "history"}]) == 6
    assert not any(key in {"trusted", "source_kind", "iframe", "html", "intent_token"} for n in nodes(view) for key in n)
    assert "apply_action" not in {a["command"] for a in view["actions"]}


def test_manifest_and_shared_frontend_fixtures_are_exact():
    manifest = json.loads((ROOT / "plugin-sdk/examples/meeting-assistant/plugin.json").read_text("utf-8"))
    assert set(manifest["commands"]) == COMMANDS
    saved = json.loads((ROOT / "frontend/lib/__fixtures__/meeting-plugin-views.json").read_text("utf-8"))
    assert saved == documents()


def test_old_features_evidence_unknown_and_read_only_are_not_lost():
    views = documents()
    updated = views["updated"]
    assert {"analysis_deactivate", "ask", "execute", "mark_create", "mark_accept", "mark_dismiss"} <= {a["command"] for a in updated["actions"]}
    anchors = [n["media_time_ms"] for n in nodes(updated) if n.get("type") == "media_anchor"]
    assert anchors and set(anchors) == {16690}
    unknown = json.dumps(views["unknown"], ensure_ascii=False)
    assert "root-1" in unknown and "exec-1" in unknown
    assert "未知" in unknown and "已确认" in unknown and "https://linear.app/demo/issue/DEMO-1" in unknown
    assert "Which release?" in json.dumps(views["needs_input"])
    assert {"input", "cancel"} <= {a["command"] for a in views["needs_input"]["actions"]}
    assert not {a["command"] for a in views["history"]["actions"]} & {"ask", "execute", "input", "cancel", "mark_create", "analysis_activate"}


def test_malicious_text_missing_time_and_large_snapshots_remain_safe():
    value = copy.deepcopy(cases()["unknown"])
    value["snapshot"]["marks"][0]["title"] = '<script>alert(1)</script>'
    value["snapshot"]["marks"][0]["audio_start_ms"] = None
    value["snapshot"]["marks"][0]["evidence_messages"][0].pop("audio_start_ms")
    value["detail"]["tool_calls"][0]["external_reference"]["url"] = "javascript:alert(1)"
    view = build_meeting_view(**value)
    parse_plugin_view(view, allowed_commands=COMMANDS)
    assert not any(n.get("type") == "safe_markdown" and "javascript:" in n.get("markdown", "") for n in nodes(view))
    marks = next(n for n in nodes(view) if n.get("id") == "marks")
    assert not any(n.get("type") == "media_anchor" for n in nodes(marks))
    big = copy.deepcopy(cases()["updated"])
    for field, key in (("marks", "mark_id"), ("candidates", "candidate_id"), ("executions", "execution_id")):
        original = big["snapshot"][field][0]
        big["snapshot"][field] = [{**copy.deepcopy(original), key: f"{field}-{i}"} for i in range(100)]
    for row in big["snapshot"]["marks"]:
        row["note"] = "大" * 64000
    parse_plugin_view(build_meeting_view(**big), allowed_commands=COMMANDS)


def test_real_host_read_model_and_detail_fit_the_plugin_view(meeting_history):
    from app.assistant.plugin_service import MeetingPluginReadService
    from app.assistant.plugin_contracts import MeetingStateQueryInput, MeetingOperationQueryInput
    from meeting_plugin_fakes import install_meeting_scope
    from meeting_assistant.contracts import state_response, operation_response
    f = meeting_history
    install_meeting_scope(f.database)
    with f.database.session() as db:
        service = MeetingPluginReadService(db)
        snapshot = state_response({"data": service.read_session(f.media_id, MeetingStateQueryInput(limit=10))}, f.media_id)
        detail = operation_response({"data": service.read_execution(f.session_id, f.child_id, MeetingOperationQueryInput(execution_id=f.child_id))}, f.session_id, execution_id=f.child_id)
    view = build_meeting_view(snapshot=snapshot, detail=detail)
    parse_plugin_view(view, allowed_commands=COMMANDS)
    rendered = json.dumps(view)
    assert "Which release?" in rendered and f.root_id in rendered


def test_fast_turn_response_text_is_visible_without_a_new_media_event():
    """The real FastTurnRunner persists response_text, not answer/summary."""
    snapshot = copy.deepcopy(cases()["updated"]["snapshot"])
    row = execution(result={"response_text": "Durable answer from the meeting assistant"})
    snapshot["executions"] = [row]
    view = build_meeting_view(snapshot=snapshot, detail={"execution": row, "events": []})
    parse_plugin_view(view, allowed_commands=COMMANDS)
    assert "Durable answer from the meeting assistant" in json.dumps(view)


def test_unresolved_assignee_notice_uses_structured_placeholder_fields_only():
    unresolved = copy.deepcopy(cases()["updated"])
    unresolved["snapshot"]["candidates"][0]["content"]["assignee"] = {
        "value": {
            "spoken_text": "小王",
            "linear_user_id": None,
            "is_placeholder": True,
        },
        "resolution": "ambiguous",
    }
    unresolved_view = build_meeting_view(**unresolved)
    parse_plugin_view(unresolved_view, allowed_commands=COMMANDS)
    assert UNRESOLVED_ASSIGNEE_NOTICE in json.dumps(unresolved_view, ensure_ascii=False)

    resolved = copy.deepcopy(unresolved)
    resolved["snapshot"]["candidates"][0]["content"]["assignee"] = {
        "value": {
            "spoken_text": "Alice",
            "linear_user_id": "linear-user-1",
            "is_placeholder": False,
        },
        "resolution": "known",
    }
    resolved_view = build_meeting_view(**resolved)
    parse_plugin_view(resolved_view, allowed_commands=COMMANDS)
    assert UNRESOLVED_ASSIGNEE_NOTICE not in json.dumps(resolved_view, ensure_ascii=False)

    result_only = copy.deepcopy(cases()["unknown"])
    result_only["snapshot"]["candidates"] = []
    result_only["detail"]["tool_calls"][0]["outcome"] = {
        "partial": True,
        "unresolved_identity": {
            "spoken_text": "小王",
            "resolution": "missing",
            "is_placeholder": True,
        },
    }
    result_view = build_meeting_view(**result_only)
    parse_plugin_view(result_view, allowed_commands=COMMANDS)
    assert UNRESOLVED_ASSIGNEE_NOTICE in json.dumps(result_view, ensure_ascii=False)
