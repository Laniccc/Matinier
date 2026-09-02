from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.hls.manager import HLSCleanupResult
from app.persistence.sessions import SessionRepository


class IdempotentFakeHLSManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, bool]] = []

    async def stop(
        self,
        session_id: str,
        *,
        graceful: bool,
        force: bool = False,
    ) -> HLSCleanupResult:
        self.calls.append((session_id, graceful, force))
        return HLSCleanupResult(
            session_id=session_id,
            outcome="stopped" if len(self.calls) == 1 else "already_stopped",
            cleanup_status="completed",
            detail="decoder=stopped,track=already_stopped,room=stopped",
        )


def test_internal_abort_requires_token_and_is_idempotent_for_terminal_session(
    client: TestClient,
) -> None:
    manager = IdempotentFakeHLSManager()
    client.app.state.hls_input_manager = manager
    room = client.post("/api/rooms", json={"display_name": "内部控制测试"}).json()
    with client.app.state.database.session() as db_session:
        repository = SessionRepository(db_session)
        record = repository.create(
            room_id=room["id"],
            room_name=room["room_name"],
            source_type="hls",
            source_name="https://media.example/live.m3u8",
            language="zh-CN",
        )
        repository.mark_source_running(record.id)
        repository.fail(
            record.id,
            error_code="asr_stream_error",
            error_message="Speech recognition stopped before completion.",
        )
        session_id = record.id
        db_session.commit()

    payload = {
        "reason": "asr_stream_error",
        "detail": "provider websocket disconnected",
        "requested_by": "caption_worker",
        "request_id": str(uuid.uuid4()),
    }
    url = f"/internal/sessions/{session_id}/source/abort"

    rejected = client.post(url, json=payload)
    assert rejected.status_code == 401
    assert manager.calls == []

    headers = {
        "X-Internal-Control-Token": client.app.state.settings.internal_control_token
    }
    first = client.post(url, json=payload, headers=headers)
    payload["request_id"] = str(uuid.uuid4())
    second = client.post(url, json=payload, headers=headers)

    assert first.status_code == 200
    assert first.json()["outcome"] == "stopped"
    assert second.status_code == 200
    assert second.json()["outcome"] == "already_stopped"
    assert manager.calls == [
        (session_id, False, True),
        (session_id, False, True),
    ]
    session = client.get(f"/api/sessions/{session_id}").json()
    assert session["status"] == "failed"
    assert session["error_code"] == "asr_stream_error"
    assert session["source_status"] == "stopped"
    assert session["cleanup_status"] == "completed"
