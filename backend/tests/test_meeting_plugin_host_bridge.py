import json
import pytest
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.api.assistant_actions import router
from app.assistant.plugin_operations import MeetingPluginOperations
from app.assistant.plugin_contracts import MeetingStateQueryInput, MeetingOperationQueryInput
from app.assistant.plugin_service import MeetingPluginReadService
from app.persistence.models import PluginPackageRecord, MeetingPluginOperationRecord, MediaSessionRecord
from app.plugins.repository import PluginRepository
from app.plugins.ui_schema import parse_plugin_view
from test_host_action_intents import make_actions, ORIGIN, MEETING_PERMISSIONS
from meeting_plugin_client_fakes import Host, load_plugin
from meeting_assistant.session import MeetingSession

PLUGIN = "com.matinier.meeting-assistant"

def ready_actions(f):
    actions, _ = make_actions(f)
    with f.database.session() as db:
        db.get(PluginPackageRecord, "meeting-package").manifest_json = json.loads((Path(__file__).parents[2] / "plugin-sdk/examples/meeting-assistant/plugin.json").read_text(encoding="utf-8"))
        repo = PluginRepository(db)
        repo.record_runtime_status(plugin_id=PLUGIN, version="1.0.0", status="ready")
        repo.publish_view(plugin_id=PLUGIN, version="1.0.0", media_session_id=f.media_id, surface="panel", view_id="meeting-assistant", view_version=1, view_json={"actions": []})
        db.commit()
    return actions

class AdmissionHost(Host):
    def __init__(self, f, actions):
        super().__init__(); self.f = f; self.operations = MeetingPluginOperations(actions)
    async def capability(self, *, name, session_scope, input_value, idempotency_key=None):
        with self.f.database.session() as db:
            service = MeetingPluginReadService(db)
            if name == "meeting.state.query":
                return {"data": service.read_session(self.f.media_id, MeetingStateQueryInput.model_validate(input_value))}
            if name == "meeting.operation.query":
                return {"data": service.operation(media_session_id=self.f.media_id, plugin_id=PLUGIN, plugin_version="1.0.0", query=MeetingOperationQueryInput.model_validate(input_value))}
            if name == "meeting.turn.submit":
                operation = self.operations.admit(db, plugin_id=PLUGIN, plugin_version="1.0.0", media_session_id=self.f.media_id, action="meeting.ask", command=input_value)
                db.commit()
                return {"status": "accepted", "operation_id": operation.id}
        return await super().capability(name=name, session_scope=session_scope, input_value=input_value, idempotency_key=idempotency_key)

def test_http_confirm_reaches_real_controller_and_durable_admission_without_secret_response(meeting_history):
    f = meeting_history; actions = ready_actions(f); host = AdmissionHost(f, actions); calls = []
    async def invoke(identity, media, scope, **kwargs):
        calls.append(kwargs)
        session = MeetingSession(host, create_task=host.create_task)
        await session.open({"media_session_id": media, "session_scope": scope, "after_sequence": 0})
        try:
            return await session.command({"media_session_id": media, "session_scope": scope, **kwargs})
        finally:
            await session.close()
    app = FastAPI(); app.state.host_actions = actions; app.state.database = f.database; app.state.settings = actions.settings
    app.state.plugin_host_runtime = SimpleNamespace(supervisor=SimpleNamespace(invoke_command=invoke)); app.include_router(router)
    with TestClient(app) as client:
        headers = {"Origin": ORIGIN, "X-Assistant-UI": "1"}
        headers["X-Assistant-UI-Nonce"] = client.post("/api/assistant-actions/ui-context", headers=headers, json={}).json()["ui_nonce"]
        prefix = f"/api/media-sessions/{f.media_id}/assistant-actions"
        controls = client.get(prefix).json()[0]
        assert {a["action"] for a in controls["actions"]} >= {"meeting.ask", "meeting.execute", "meeting.input", "meeting.cancel", "meeting.mark.create", "meeting.mark.accept", "meeting.mark.dismiss"}
        preview = client.post(prefix + "/prepare", headers=headers, json={"action": "meeting.ask", "request_id": "http-one", "plugin_version": "1.0.0", "view_version": 1, "arguments": {"message": "Explain release notes"}})
        assert preview.status_code == 200, preview.text
        value = preview.json(); confirm = {"preview_id": value["preview_id"], "preview_hash": value["preview_hash"], "confirmed": True}
        first = client.post(prefix + "/confirm", headers=headers, json=confirm)
        assert first.status_code == 200 and first.json()["status"] == "accepted", first.text
        assert client.post(prefix + "/confirm", headers=headers, json=confirm).json() == first.json()
        assert "intent_token" not in first.text and "command" not in first.text
        assert len(calls) == 1
    with f.database.session() as db:
        assert db.scalar(select(func.count(MeetingPluginOperationRecord.id))) == 1

def test_history_http_and_descriptor_are_read_only_and_scope_bound(meeting_history):
    f = meeting_history; actions, _ = make_actions(f, assistant_enabled=False, plugin_framework_enabled=False)
    app = FastAPI(); app.state.host_actions = actions; app.state.database = f.database; app.state.settings = actions.settings; app.include_router(router)
    with TestClient(app) as client:
        sources = client.get(f"/api/sessions/{f.session_id}/assistant-history-sources").json()
        assert len(sources) == 1
        page = client.get(f"/api/media-sessions/{f.media_id}/assistant-history").json()
        parse_plugin_view(page["ui_view"], allowed_commands=frozenset())
        controls = client.get(f"/api/media-sessions/{f.media_id}/assistant-actions").json()[0]
        assert controls["source_kind"] == "host_history"
        assert [a["action"] for a in controls["actions"]] == ["meeting.cancel"]
        assert client.get(f"/api/media-sessions/{f.other_media_id}/assistant-history?execution_id={f.child_id}").status_code == 403
        assert client.get(f"/api/media-sessions/{f.media_id}/assistant-history?plugin_id=unknown").status_code == 404

def test_unbridged_legacy_history_does_not_disappear_or_write_on_get(meeting_history):
    f = meeting_history
    with f.database.session() as db:
        db.delete(db.get(MediaSessionRecord, f.media_id)); db.commit()
    from app.assistant.plugin_history import MeetingPluginHistory
    with f.database.session() as db:
        history = MeetingPluginHistory(db)
        sources = history.sources(f.session_id)
        assert len(sources) == 1
        page = history.page(sources[0]["media_session_id"], MeetingStateQueryInput())
        assert page["view"]["executions"]
        assert db.get(MediaSessionRecord, f.media_id) is None and not db.new and not db.dirty

def test_unbridged_history_cancel_uses_real_mapping_only_after_explicit_action(meeting_history):
    f = meeting_history; actions, _ = make_actions(f, assistant_enabled=False, plugin_framework_enabled=False)
    with f.database.session() as db:
        db.delete(db.get(MediaSessionRecord, f.media_id)); db.commit()
    app = FastAPI(); app.state.host_actions = actions; app.state.database = f.database; app.state.settings = actions.settings; app.include_router(router)
    with TestClient(app) as client:
        headers = {"Origin": ORIGIN, "X-Assistant-UI": "1"}
        headers["X-Assistant-UI-Nonce"] = client.post("/api/assistant-actions/ui-context", headers=headers, json={}).json()["ui_nonce"]
        alias = "legacy:" + f.session_id
        prefix = f"/api/media-sessions/{alias}"
        control = client.get(prefix + "/assistant-actions").json()[0]["actions"][0]
        target = next(o for o in control["fields"][0]["options"] if o["value"] == f.child_id)
        preview = client.post(prefix + "/assistant-history/prepare-cancel", headers=headers,
            json={"action": "meeting.cancel", "request_id": "cancel-old", "arguments": {"execution_id": f.child_id, **target["arguments"]}})
        assert preview.status_code == 200, preview.text
        value = preview.json()
        response = client.post(prefix + "/assistant-actions/confirm", headers=headers,
            json={"preview_id": value["preview_id"], "preview_hash": value["preview_hash"], "confirmed": True})
        assert response.json()["status"] == "accepted", response.text
    with f.database.session() as db:
        from app.persistence.models import AssistantExecutionRecord
        assert db.get(AssistantExecutionRecord, f.child_id).status == "cancelled"
        operation = db.scalar(select(MeetingPluginOperationRecord))
        assert not operation.media_session_id.startswith("legacy:")

def test_history_view_stays_bounded_for_large_records():
    from app.assistant.plugin_history_view import history_document
    row = {"goal": "x" * 4000, "result": {"response_text": "y" * 32000, "external_reference": {"url": "https://example.com/" + "a" * 900}}, "audio_start_ms": 123}
    display = {key: [row] * 50 for key in ("marks", "candidates", "executions", "events")}
    document = history_document(display, 1)
    parse_plugin_view(document, allowed_commands=frozenset())

def test_analysis_confirmation_explicitly_wakes_plugin_and_catchup(meeting_history):
    f = meeting_history; actions = ready_actions(f); calls = []
    async def invoke(*args, **kwargs): calls.append(kwargs["command"]); return {"refreshed": True}
    async def catchup(session_id): calls.append(session_id)
    app = FastAPI(); app.state.host_actions = actions
    app.state.plugin_host_runtime = SimpleNamespace(supervisor=SimpleNamespace(invoke_command=invoke))
    app.state.meeting_state_projector = SimpleNamespace(request_catch_up=catchup); app.include_router(router)
    with TestClient(app) as client:
        headers = {"Origin": ORIGIN, "X-Assistant-UI": "1"}
        headers["X-Assistant-UI-Nonce"] = client.post("/api/assistant-actions/ui-context", headers=headers, json={}).json()["ui_nonce"]
        prefix = f"/api/media-sessions/{f.media_id}/assistant-actions"
        preview = client.post(prefix + "/prepare", headers=headers, json={"action": "meeting.analysis.activate", "request_id": "activate", "plugin_version": "1.0.0", "view_version": 1, "arguments": {}}).json()
        response = client.post(prefix + "/confirm", headers=headers, json={"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"], "confirmed": True})
        assert response.json()["status"] == "applied"
        assert calls == [f.session_id, "refresh"]

@pytest.mark.parametrize("changes", [{"assistant_enabled": False}, {"plugin_framework_enabled": False}, {"task_system_provider": "disabled"}])
def test_configuration_never_offers_unavailable_external_actions(meeting_history, changes):
    f = meeting_history; actions = ready_actions(f); actions.settings = actions.settings.model_copy(update=changes)
    from app.plugins.host_action_descriptors import describe_actions
    controls = describe_actions(actions, f.media_id)[0]["actions"]
    assert not any(c["enabled"] for c in controls if c["action"] == "meeting.execute")
