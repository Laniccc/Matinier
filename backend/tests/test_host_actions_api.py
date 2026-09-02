import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.assistant_actions import router
from test_host_action_intents import make_actions, ORIGIN


def make_client(f):
    actions, _ = make_actions(f)
    app = FastAPI()
    app.state.database = f.database
    app.state.settings = actions.settings
    app.state.host_actions = actions
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("headers", [
    {}, {"Origin": ORIGIN}, {"Origin": "https://evil.example", "X-Assistant-UI": "1"},
    {"Origin": "null", "X-Assistant-UI": "1"}, {"X-Assistant-UI": "1"},
])
def test_ui_context_requires_exact_origin_and_custom_header(meeting_history, headers):
    with make_client(meeting_history) as client:
        assert client.post("/api/assistant-actions/ui-context", json={}, headers=headers).status_code == 403


@pytest.mark.parametrize("extra", [
    {"actor_id": "intruder"}, {"session_id": "other"}, {"scope": {}},
    {"trusted": True}, {"grant": {}}, {"plugin_version": "2.0.0"},
    {"arguments": {"message": "Ask", "team_id": "other"}},
])
def test_prepare_rejects_forged_context_or_arguments(meeting_history, extra):
    f = meeting_history
    with make_client(f) as client:
        headers = {"Origin": ORIGIN, "X-Assistant-UI": "1"}
        nonce = client.post("/api/assistant-actions/ui-context", json={}, headers=headers).json()["ui_nonce"]
        headers["X-Assistant-UI-Nonce"] = nonce
        response = client.post(f"/api/media-sessions/{f.media_id}/assistant-actions/prepare", headers=headers,
            json={"action": "meeting.ask", "request_id": "request-1", "plugin_version": "1.0.0", "arguments": {"message": "Ask"}, **extra})
        assert response.status_code in {403, 422}


def test_plugin_confirmation_json_is_not_host_authorization(meeting_history):
    f = meeting_history
    with make_client(f) as client:
        response = client.post(f"/api/media-sessions/{f.media_id}/assistant-actions/prepare",
            headers={"Origin": ORIGIN, "X-Assistant-UI": "1"},
            json={"action": "meeting.ask", "request_id": "request-1", "arguments": {"message": "Ask"}})
        assert response.status_code == 403
