import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.assistant import router as assistant
from app.api.meeting_state import router as meeting
from app.settings import Settings

def client_for(f):
    app = FastAPI()
    app.state.database = f.database
    app.state.settings = Settings(_env_file=None)
    app.state.assistant_runtime = None
    app.include_router(assistant); app.include_router(meeting)
    return TestClient(app)

@pytest.mark.parametrize("method,path", [
    ("post", "/api/sessions/meeting-a/assistant/turns"),
    ("post", "/api/assistant/executions/meeting-child/input"),
    ("post", "/api/assistant/executions/meeting-child/cancel"),
    ("post", "/api/sessions/meeting-a/marks"),
    ("patch", "/api/sessions/meeting-a/marks/automatic-mark"),
])
def test_legacy_writes_are_closed_before_payload_or_runtime_admission(meeting_history, method, path):
    with client_for(meeting_history) as client:
        result = getattr(client, method)(path, json={"actor_id": "forged", "grant": {"max_side_effects": 99}})
        assert result.status_code == 410
        assert result.json()["detail"]["code"] == "meeting_plugin_migration_required"

@pytest.mark.parametrize("path", [
    "/api/sessions/meeting-a/assistant/state", "/api/sessions/meeting-a/assistant/events",
    "/api/assistant/executions/meeting-child", "/api/sessions/meeting-a/meeting-state", "/api/sessions/meeting-a/marks",
])
def test_legacy_reads_work_with_runtime_disabled(meeting_history, path):
    with client_for(meeting_history) as client:
        assert client.get(path).status_code == 200
