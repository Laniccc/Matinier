from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi.testclient import TestClient
from livekit import api

from app.hls.manager import HLSCleanupResult, HLSStartRequest
from app.persistence.sessions import SessionRepository


class FakeRoomAdmin:
    def __init__(self) -> None:
        self.deleted_rooms: list[str] = []
        self.removed_participants: list[tuple[str, str]] = []
        self.ensured_dispatches: list[tuple[str, str]] = []
        self.close_count = 0

    async def ensure_agent_dispatch(
        self,
        room_name: str,
        agent_name: str,
    ) -> None:
        self.ensured_dispatches.append((room_name, agent_name))

    async def delete_room(self, room_name: str) -> None:
        self.deleted_rooms.append(room_name)

    async def remove_participant(
        self,
        room_name: str,
        participant_identity: str,
    ) -> None:
        self.removed_participants.append((room_name, participant_identity))

    async def aclose(self) -> None:
        self.close_count += 1


class FakeHLSInputManager:
    def __init__(self) -> None:
        self.started: list[HLSStartRequest] = []
        self.stopped: list[tuple[str, bool, bool]] = []
        self.stopped_rooms: list[tuple[str, bool]] = []
        self.on_stop: Callable[[str], Awaitable[None]] | None = None

    async def start(self, request: HLSStartRequest) -> None:
        self.started.append(request)

    async def stop(
        self,
        session_id: str,
        *,
        graceful: bool,
        force: bool = False,
    ) -> HLSCleanupResult:
        self.stopped.append((session_id, graceful, force))
        if self.on_stop is not None:
            await self.on_stop(session_id)
        return HLSCleanupResult(
            session_id=session_id,
            outcome="stopped",
            cleanup_status="completed",
            detail="fake source stopped",
        )

    async def stop_for_room(
        self,
        room_id: str,
        *,
        graceful: bool,
    ) -> None:
        self.stopped_rooms.append((room_id, graceful))


def test_room_lifecycle_and_reusable_caption_runs(client: TestClient) -> None:
    fake_admin = FakeRoomAdmin()
    client.app.state.room_admin_factory = lambda _settings: fake_admin
    created_response = client.post(
        "/api/rooms",
        json={"display_name": "主直播间"},
    )
    assert created_response.status_code == 201
    room = created_response.json()
    assert room["status"] == "ready"
    assert room["display_name"] == "主直播间"
    assert room["room_name"].startswith("test-room-room-")

    renamed = client.patch(
        f"/api/rooms/{room['id']}",
        json={"display_name": "产品发布会"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "产品发布会"

    first_response = client.post(
        f"/api/rooms/{room['id']}/caption-runs",
        json={
            "source_type": "microphone",
            "source_name": "浏览器麦克风",
            "language": "zh-CN",
            "target_language": "en-US",
        },
    )
    assert first_response.status_code == 201
    first = first_response.json()
    assert first["room_name"] == room["room_name"]
    assert first["language"] == "zh-CN"
    assert first["target_language"] == "en-US"
    assert first["translation_status"] == "starting"
    assert fake_admin.ensured_dispatches == [
        (room["room_name"], "live-caption-agent")
    ]

    conflict = client.post(
        f"/api/rooms/{room['id']}/caption-runs",
        json={
            "source_type": "file",
            "source_name": "sample.mp3",
            "language": "zh-CN",
        },
    )
    assert conflict.status_code == 409

    cancelled = client.post(
        f"/api/rooms/{room['id']}/caption-runs/{first['id']}/cancel",
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    second_response = client.post(
        f"/api/rooms/{room['id']}/caption-runs",
        json={
            "source_type": "file",
            "source_name": "sample.mp3",
            "language": "zh-CN",
        },
    )
    assert second_response.status_code == 201
    second = second_response.json()
    assert second["room_name"] == room["room_name"]
    assert second["id"] != first["id"]
    assert fake_admin.ensured_dispatches == [
        (room["room_name"], "live-caption-agent"),
        (room["room_name"], "live-caption-agent"),
    ]

    runs = client.get(f"/api/rooms/{room['id']}/caption-runs")
    assert runs.status_code == 200
    assert [item["id"] for item in runs.json()] == [second["id"], first["id"]]
    assert client.get("/api/rooms").json()[0]["id"] == room["id"]


def test_room_operator_token_can_publish_and_subscribe(client: TestClient) -> None:
    room = client.post("/api/rooms", json={"display_name": "调试间"}).json()
    response = client.post(
        f"/api/rooms/{room['id']}/token",
        json={"participant_identity": "operator-alice"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["participant_identity"] == "operator-alice"
    assert body["room_name"] == room["room_name"]
    claims = api.TokenVerifier(
        api_key="test-key",
        api_secret="test-secret-that-is-at-least-32-bytes",
    ).verify(body["token"])
    assert claims.video is not None
    assert claims.video.room == room["room_name"]
    assert claims.video.can_publish is True
    assert claims.video.can_subscribe is True
    assert claims.video.can_publish_data is True


def test_room_admin_close_and_remove_participant(client: TestClient) -> None:
    fake_admin = FakeRoomAdmin()
    client.app.state.room_admin_factory = lambda _settings: fake_admin
    room = client.post("/api/rooms", json={"display_name": "管理测试"}).json()
    run = client.post(
        f"/api/rooms/{room['id']}/caption-runs",
        json={
            "source_type": "screen",
            "source_name": "标签页音频",
            "language": "zh-CN",
        },
    ).json()

    removed = client.post(
        f"/api/rooms/{room['id']}/participants/guest-1/remove"
    )
    assert removed.status_code == 204
    assert fake_admin.removed_participants == [(room["room_name"], "guest-1")]

    closed = client.post(f"/api/rooms/{room['id']}/close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert fake_admin.deleted_rooms == [room["room_name"]]
    assert fake_admin.close_count == 3
    assert client.get(f"/api/sessions/{run['id']}").json()["status"] == "cancelled"

    rejected = client.post(
        f"/api/rooms/{room['id']}/caption-runs",
        json={
            "source_type": "microphone",
            "source_name": "麦克风",
            "language": "zh-CN",
        },
    )
    assert rejected.status_code == 409


def test_unknown_room_returns_404(client: TestClient) -> None:
    response = client.get("/api/rooms/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["detail"] == "Room not found"


def test_room_hls_input_start_conflict_stop_and_close(
    client: TestClient,
    monkeypatch,
) -> None:
    from app.hls.url_policy import ValidatedHLSURL

    async def validate_test_url(
        value: str,
        **_kwargs,
    ) -> ValidatedHLSURL:
        return ValidatedHLSURL(
            fetch_url=value.removesuffix("#player"),
            display_url="https://93.184.216.34/live/index.m3u8",
            resolved_host="93.184.216.34",
            resolved_ips=("93.184.216.34",),
            redirect_count=0,
        )

    monkeypatch.setattr("app.api.rooms.validate_hls_url", validate_test_url)
    fake_hls = FakeHLSInputManager()
    fake_admin = FakeRoomAdmin()
    client.app.state.hls_input_manager = fake_hls
    client.app.state.room_admin_factory = lambda _settings: fake_admin
    room = client.post("/api/rooms", json={"display_name": "公网直播"}).json()
    full_url = (
        "https://93.184.216.34/live/index.m3u8"
        "?token=must-not-be-persisted#player"
    )

    started = client.post(
        f"/api/rooms/{room['id']}/hls-inputs",
        json={"url": full_url, "language": "ja-JP"},
    )
    assert started.status_code == 201
    run = started.json()
    assert run["source_type"] == "hls"
    assert run["source_name"] == (
        "https://93.184.216.34/live/index.m3u8"
    )
    assert fake_hls.started == [
        HLSStartRequest(
            session_id=run["id"],
            room_id=room["id"],
            room_name=room["room_name"],
            fetch_url=full_url.removesuffix("#player"),
        )
    ]

    conflict = client.post(
        f"/api/rooms/{room['id']}/hls-inputs",
        json={"url": full_url, "language": "ja-JP"},
    )
    assert conflict.status_code == 409

    async def complete_while_hls_manager_stops(session_id: str) -> None:
        with client.app.state.database.session() as db_session:
            sessions = SessionRepository(db_session)
            sessions.start_replay(session_id)
            sessions.begin_finalizing(session_id)
            sessions.complete(
                session_id,
                final_result_count=1,
                first_partial_latency_ms=100.0,
                average_final_latency_ms=200.0,
                provider_error_count=0,
                sent_audio_chunk_count=10,
                sent_audio_bytes=6_400,
            )
            sessions.mark_source_stopped(session_id)
            db_session.commit()

    fake_hls.on_stop = complete_while_hls_manager_stops

    stopped = client.post(
        f"/api/rooms/{room['id']}/hls-inputs/{run['id']}/stop",
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "completed"
    assert stopped.json()["stop_reason"] == "user_requested"
    assert fake_hls.stopped == [(run["id"], True, True)]

    closed = client.post(f"/api/rooms/{room['id']}/close")
    assert closed.status_code == 200
    assert fake_hls.stopped_rooms == [(room["id"], False)]
