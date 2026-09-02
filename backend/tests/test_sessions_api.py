from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.persistence.models import SessionRecord


def test_create_and_get_session(client: TestClient) -> None:
    created_response = client.post(
        "/api/sessions",
        json={"source_name": "empty-room-demo", "language": "zh-CN"},
    )

    assert created_response.status_code == 201
    created = created_response.json()
    assert created["room_name"].startswith("test-room-")
    assert created["status"] == "created"
    assert created["source_type"] == "empty"
    assert created["source_name"] == "empty-room-demo"
    assert created["language"] == "zh-CN"
    assert created["target_language"] is None
    assert created["translation_status"] == "disabled"
    assert created["started_at"] is None
    assert created["ended_at"] is None
    assert created["asr_provider"] is None
    assert created["asr_model"] is None
    assert created["final_result_count"] is None
    assert created["first_partial_latency_ms"] is None
    assert created["average_final_latency_ms"] is None
    assert created["provider_error_count"] is None
    assert created["sent_audio_chunk_count"] is None
    assert created["sent_audio_bytes"] is None
    assert created["error_code"] is None
    assert created["error_message"] is None

    fetched_response = client.get(f"/api/sessions/{created['id']}")
    assert fetched_response.status_code == 200
    assert fetched_response.json() == created
    translations = client.get(
        f"/api/sessions/{created['id']}/translations"
    )
    assert translations.status_code == 200
    assert translations.json() == []


def test_unknown_session_returns_404(client: TestClient) -> None:
    response = client.get("/api/sessions/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def test_runtime_endpoint_merges_safe_worker_snapshot(client: TestClient) -> None:
    created = client.post(
        "/api/sessions",
        json={"source_name": "runtime-demo", "language": "zh-CN"},
    ).json()
    snapshot = {
        "room_connected": True,
        "track_subscribed": True,
        "asr_connected": True,
        "translation_connected": False,
        "audio_bytes": 64_000,
        "audio_frames": 100,
        "audio_queue_current": 2,
        "audio_queue_max": 5,
        "last_event_at": "2026-08-03T08:00:00Z",
    }
    reported = client.post(
        f"/internal/sessions/{created['id']}/runtime",
        json=snapshot,
        headers={
            "X-Internal-Control-Token": (
                client.app.state.settings.internal_control_token
            )
        },
    )
    assert reported.status_code == 200

    response = client.get(f"/api/sessions/{created['id']}/runtime")
    assert response.status_code == 200
    runtime = response.json()
    assert runtime["session_status"] == "created"
    assert runtime["source_status"] == "starting"
    assert runtime["room_connected"] is True
    assert runtime["track_subscribed"] is True
    assert runtime["asr_connected"] is True
    assert runtime["audio_bytes"] == 64_000
    assert runtime["audio_frames"] == 100
    assert runtime["source_final_count"] == 0
    assert runtime["translation_final_count"] == 0
    assert "publisher_connected" in runtime["unavailable"]
    assert "track_published" in runtime["unavailable"]
    assert "ffmpeg_running" in runtime["unavailable"]


def test_file_replays_create_distinct_sessions(client: TestClient) -> None:
    payload = {
        "source_type": "file",
        "source_name": "demo_audio.wav",
        "language": "zh-CN",
    }

    first = client.post("/api/sessions", json=payload)
    second = client.post("/api/sessions", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["source_type"] == "file"
    assert second.json()["source_type"] == "file"
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["room_name"] != second.json()["room_name"]


def test_session_history_is_newest_first_and_includes_persisted_metrics(
    client: TestClient,
) -> None:
    first = client.post(
        "/api/sessions",
        json={"source_type": "file", "source_name": "first.wav", "language": "zh-CN"},
    ).json()
    second = client.post(
        "/api/sessions",
        json={"source_type": "file", "source_name": "second.wav", "language": "zh-CN"},
    ).json()
    with Session(client.app.state.database.engine) as db_session:
        first_record = db_session.get(SessionRecord, first["id"])
        second_record = db_session.get(SessionRecord, second["id"])
        assert first_record is not None
        assert second_record is not None
        first_record.created_at = dt.datetime(2026, 7, 22, 10, tzinfo=dt.UTC)
        second_record.created_at = dt.datetime(2026, 7, 22, 11, tzinfo=dt.UTC)
        second_record.status = "completed"
        second_record.asr_provider = "bailian"
        second_record.asr_model = "fun-asr-realtime"
        second_record.final_result_count = 1
        second_record.first_partial_latency_ms = 125.0
        second_record.average_final_latency_ms = 700.0
        second_record.provider_error_count = 0
        second_record.sent_audio_chunk_count = 40
        second_record.sent_audio_bytes = 128_000
        second_record.error_code = None
        second_record.error_message = None
        db_session.commit()

    response = client.get("/api/sessions")

    assert response.status_code == 200
    history = response.json()
    assert [item["id"] for item in history] == [second["id"], first["id"]]
    assert history[0]["asr_provider"] == "bailian"
    assert history[0]["asr_model"] == "fun-asr-realtime"
    assert history[0]["final_result_count"] == 1
    assert history[0]["average_final_latency_ms"] == 700.0
    assert history[0]["sent_audio_bytes"] == 128_000


def test_replay_failure_and_cancellation_are_sanitized_and_terminal(
    client: TestClient,
) -> None:
    failed_session = client.post(
        "/api/sessions",
        json={
            "source_type": "file",
            "source_name": "bad.wav",
            "language": "zh-CN",
        },
    ).json()
    failed = client.post(
        f"/api/sessions/{failed_session['id']}/replay-status",
        json={"status": "failed", "error_code": "media_decode_error"},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "media_decode_error"
    assert (
        failed.json()["error_message"]
        == "The audio file could not be decoded."
    )

    cannot_regress = client.post(
        f"/api/sessions/{failed_session['id']}/replay-status",
        json={"status": "cancelled"},
    )
    assert cannot_regress.status_code == 409

    cancelled_session = client.post(
        "/api/sessions",
        json={
            "source_type": "file",
            "source_name": "cancelled.wav",
            "language": "zh-CN",
        },
    ).json()
    cancelled = client.post(
        f"/api/sessions/{cancelled_session['id']}/replay-status",
        json={"status": "cancelled"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["error_code"] is None
    assert cancelled.json()["error_message"] is None


def test_replay_terminal_report_rejects_raw_or_unknown_errors(
    client: TestClient,
) -> None:
    session = client.post(
        "/api/sessions",
        json={"source_type": "file", "source_name": "bad.wav"},
    ).json()
    response = client.post(
        f"/api/sessions/{session['id']}/replay-status",
        json={
            "status": "failed",
            "error_code": "private_stack_trace",
            "message": "E:/secret/input.wav",
        },
    )
    assert response.status_code == 422
