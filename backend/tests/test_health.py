from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "process": "api"}

    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok", "process": "api"}


def test_readiness_checks_database_and_persistent_directories(
    tmp_path,
    monkeypatch,
) -> None:
    from app.main import create_app
    from app.persistence.database import Database
    from app.settings import Settings

    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        data_dir=tmp_path,
        database_url="sqlite://",
    )
    database = Database("sqlite://")
    database.create_schema()

    with TestClient(
        create_app(settings=settings, database=database)
    ) as test_client:
        ready = test_client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.json()["plugins"]["framework_enabled"] is True
        assert ready.json()["plugins"]["container_runtime_available"] is False
        assert ready.json()["assistant"] == {
            "enabled": False,
            "runtime_started": False,
            "projector": {
                "status": "disabled",
                "session_count": 0,
                "pending_segment_count": 0,
                "worst_lag_ms": 0,
                "last_success_at": None,
            },
            "fast_turn_queue_depth": 0,
            "fast_turn_active_count": 0,
            "action_run_queue_depth": 0,
            "action_run_active_count": 0,
            "action_run_oldest_queued_age_seconds": None,
            "tool_call_unknown_count": 0,
            "tool_call_reconciling_count": 0,
            "recovery_backlog_count": 0,
            "task_adapter_provider": "disabled",
            "task_adapter_available": False,
        }
        assert all(
            check["ok"] for check in ready.json()["checks"].values()
        )

        def fail_database_check() -> None:
            raise OSError("private database detail")

        monkeypatch.setattr(database, "check_read_write", fail_database_check)
        unavailable = test_client.get("/health/ready")
        assert unavailable.status_code == 503
        assert unavailable.json()["status"] == "not_ready"
        assert unavailable.json()["checks"]["database"] == {
            "ok": False,
            "detail": "unavailable:OSError",
        }
        assert "private database detail" not in unavailable.text
