from __future__ import annotations

from fastapi.testclient import TestClient
from livekit import api


def test_token_is_scoped_to_the_session_room(client: TestClient) -> None:
    session = client.post("/api/sessions", json={}).json()

    response = client.post(
        "/api/livekit/token",
        json={
            "session_id": session["id"],
            "participant_identity": "browser-alice",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "ws://127.0.0.1:7880"
    assert body["room_name"] == session["room_name"]
    assert body["participant_identity"] == "browser-alice"

    claims = api.TokenVerifier(
        api_key="test-key",
        api_secret="test-secret-that-is-at-least-32-bytes",
    ).verify(body["token"])
    assert claims.identity == "browser-alice"
    assert claims.video is not None
    assert claims.video.room_join is True
    assert claims.video.room == session["room_name"]
    assert claims.video.can_publish is False
    assert claims.video.can_subscribe is True
    assert claims.video.can_publish_data is True
    restored = client.get(f"/api/sessions/{session['id']}").json()
    assert restored["status"] == "starting"


def test_token_for_unknown_session_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/livekit/token",
        json={"session_id": "00000000-0000-0000-0000-000000000000"},
    )

    assert response.status_code == 404


def test_replay_token_has_server_derived_identity_and_audio_publish_grant(
    client: TestClient,
) -> None:
    session = client.post(
        "/api/sessions",
        json={"source_type": "file", "source_name": "demo_audio.wav"},
    ).json()

    response = client.post(
        "/api/livekit/token",
        json={
            "session_id": session["id"],
            "participant_type": "replay",
            "participant_identity": "caller-cannot-override-this",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["participant_identity"] == f"replay-{session['id']}"

    claims = api.TokenVerifier(
        api_key="test-key",
        api_secret="test-secret-that-is-at-least-32-bytes",
    ).verify(body["token"])
    assert claims.identity == f"replay-{session['id']}"
    assert claims.video is not None
    assert claims.video.room == session["room_name"]
    assert claims.video.can_publish is True
    assert claims.video.can_publish_sources == ["microphone"]
    assert claims.video.can_subscribe is False
    assert claims.video.can_publish_data is False
    restored = client.get(f"/api/sessions/{session['id']}").json()
    assert restored["status"] == "starting"


def test_repeated_token_does_not_regress_a_later_session_state(
    client: TestClient,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    first = client.post(
        "/api/livekit/token",
        json={"session_id": session["id"]},
    )
    assert first.status_code == 200

    from sqlalchemy.orm import Session

    from app.persistence.models import SessionRecord

    with Session(client.app.state.database.engine) as db_session:
        record = db_session.get(SessionRecord, session["id"])
        assert record is not None
        record.status = "running"
        db_session.commit()

    second = client.post(
        "/api/livekit/token",
        json={"session_id": session["id"]},
    )
    assert second.status_code == 200
    restored = client.get(f"/api/sessions/{session['id']}").json()
    assert restored["status"] == "running"
