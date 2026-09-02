from app.main import create_app
from app.settings import Settings
import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from app.assistant.tools import ToolRegistry
from app.assistant.plugin_policy import MeetingPluginPolicy
from app.plugins.repository import PluginRepository
from test_meeting_plugin_host_bridge import ready_actions

def test_composition_has_one_authority_service_and_closed_startup_gate(meeting_history):
    app = create_app(settings=Settings(_env_file=None, assistant_enabled=True, plugin_framework_enabled=True, task_system_provider="fake"), database=meeting_history.database)
    actions = app.state.host_actions
    worker = app.state.assistant_runtime.operation_worker
    assert app.state.plugin_host_runtime.meeting_operations is worker.service
    assert worker.service.actions is actions
    assert app.state.assistant_runtime.projector._policy is app.state.meeting_plugin_policy
    assert not actions.accepting
    assert not app.state.meeting_plugin_policy.enabled
    paths = set(app.openapi()["paths"])
    assert "/api/media-sessions/{media_id}/assistant-actions/confirm" in paths
    assert "/api/sessions/{session_id}/assistant-history-sources" in paths

@pytest.mark.parametrize("fail_plugin", [False, True])
def test_lifecycle_keeps_gate_closed_until_restore_and_closes_before_shutdown(meeting_history, fail_plugin):
    observations = []; app = None
    def record(name): observations.append((name, app.state.host_actions.accepting, app.state.meeting_plugin_policy.enabled))
    async def plugin_start():
        record("plugin.start")
        if fail_plugin: raise RuntimeError("fixture startup failure")
    async def plugin_stop(): record("plugin.stop")
    async def assistant_start(): record("assistant.start"); return SimpleNamespace(enqueued_execution_ids=[])
    async def assistant_stop(): record("assistant.stop")
    async def start_jobs(): return 0
    async def noop(): pass
    assistant = SimpleNamespace(registry=ToolRegistry(), projector=object(), start=assistant_start, stop=assistant_stop)
    app = create_app(settings=Settings(_env_file=None, plugin_framework_enabled=True), database=meeting_history.database,
        assistant_runtime=assistant, plugin_host_runtime=SimpleNamespace(start=plugin_start, stop=plugin_stop),
        processing_job_runner=SimpleNamespace(start=start_jobs, stop=noop), hls_input_manager=SimpleNamespace(reconcile_orphans=start_jobs, aclose=noop))
    with TestClient(app):
        assert app.state.host_actions.accepting
        assert app.state.meeting_plugin_policy.enabled is (not fail_plugin)
    assert observations[0] == ("plugin.start", False, False)
    assert observations[-2:] == [("assistant.stop", False, False), ("plugin.stop", False, False)]

@pytest.mark.parametrize("status", ["ready", "degraded", "crashed", "quarantined", "stopped"])
def test_production_analysis_gate_requires_live_plugin_health(meeting_history, status):
    f = meeting_history; ready_actions(f)
    policy = MeetingPluginPolicy(f.database, require_runtime_ready=True)
    policy.activate(f.session_id, f.media_id, plugin_version="1.0.0", actor_id="local-user")
    with f.database.session() as db:
        PluginRepository(db).record_runtime_status(plugin_id="com.matinier.meeting-assistant", version="1.0.0", status=status); db.commit()
    assert policy.admission(f.session_id).allowed is (status in {"ready", "degraded"})
