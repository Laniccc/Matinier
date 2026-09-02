from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.plugins.bootstrap import PluginHostRuntime
from app.plugins.repository import PluginRepository
from app.settings import Settings


class FakeMediaPluginRuntime:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def health_snapshot(self) -> dict[str, object]:
        return {
            "framework_enabled": True,
            "container_runtime_available": True,
            "installed_count": 1,
            "enabled_count": 1,
            "ready_count": 1,
            "quarantined_count": 0,
            "media_projector_status": "running",
            "media_projector_lag": 0,
            "rpc_pending_count": 0,
        }

    async def resolve_media_session(self, legacy_session_id: str) -> dict[str, object]:
        if legacy_session_id != "legacy-1":
            raise LookupError("missing")
        return {
            "media_session_id": "media-1",
            "legacy_session_id": legacy_session_id,
            "mode": "live",
            "source_kind": "browser-tab",
            "status": "active",
        }

    def list_media_events(
        self, media_session_id: str, *, after_sequence: int, limit: int
    ) -> list[dict[str, object]]:
        assert media_session_id == "media-1"
        assert after_sequence == 4
        assert limit == 20
        return [
            {
                "event_id": "event-5",
                "session_id": "media-1",
                "sequence": 5,
                "schema_version": 1,
                "event_type": "transcript.final",
                "finality": "final",
                "source": "host.transcription",
                "payload": {"text": "hello"},
                "created_at": "2026-08-28T10:00:00Z",
            }
        ]

    def list_plugin_views(self, media_session_id: str) -> list[dict[str, object]]:
        assert media_session_id == "media-1"
        return [
            {
                "plugin_id": "com.example.viewer",
                "plugin_version": "1.0.0",
                "session_scope": "scope-owned",
                "surface": "panel",
                "view_id": "main",
                "view_version": 2,
                "view": {
                    "schema_version": 1,
                    "surface": "panel",
                    "view_id": "main",
                    "view_version": 2,
                    "root": {"id": "ready", "type": "text", "text": "Ready"},
                    "actions": [],
                },
            }
        ]

    async def execute_plugin_command(
        self, media_session_id: str, **values: object
    ) -> dict[str, object]:
        if values["plugin_id"] != "com.example.viewer":
            raise PermissionError("scope belongs to another plugin")
        assert media_session_id == "media-1"
        assert values["session_scope"] == "scope-owned"
        assert values["expected_view_version"] == 2
        return {"accepted": True, "command_id": "command-1"}


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        database_url="sqlite://",
        data_dir=tmp_path / "data",
    )


def test_media_bridge_events_views_and_scoped_commands(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    runtime = FakeMediaPluginRuntime()
    with TestClient(
        create_app(
            settings=settings(tmp_path),
            database=database,
            plugin_host_runtime=runtime,
        )
    ) as client:
        bridge = client.get("/api/sessions/legacy-1/media-session")
        assert bridge.status_code == 200
        assert bridge.json()["media_session_id"] == "media-1"

        events = client.get("/api/media-sessions/media-1/events?after=4&limit=20")
        assert events.status_code == 200
        assert events.json()[0]["sequence"] == 5

        views = client.get("/api/media-sessions/media-1/plugin-views")
        assert views.status_code == 200
        assert views.json()[0]["session_scope"] == "scope-owned"

        command = client.post(
            "/api/media-sessions/media-1/plugin-commands",
            json={
                "plugin_id": "com.example.viewer",
                "plugin_version": "1.0.0",
                "session_scope": "scope-owned",
                "surface": "panel",
                "view_id": "main",
                "expected_view_version": 2,
                "action_id": "run",
                "values": {},
            },
        )
        assert command.json() == {"accepted": True, "command_id": "command-1"}

        cross_plugin = client.post(
            "/api/media-sessions/media-1/plugin-commands",
            json={
                "plugin_id": "com.example.attacker",
                "plugin_version": "1.0.0",
                "session_scope": "scope-owned",
                "surface": "panel",
                "view_id": "main",
                "expected_view_version": 2,
                "action_id": "run",
                "values": {},
            },
        )
        assert cross_plugin.status_code == 403
        assert "scope belongs" not in cross_plugin.text

        missing = client.get("/api/sessions/missing/media-session")
        assert missing.status_code == 404


def test_runtime_generates_command_id_before_supervisor_rpc(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    runtime = PluginHostRuntime(settings(tmp_path), database)
    calls: list[dict[str, object]] = []

    async def invoke_command(_identity, _media_id, _scope, **values):
        calls.append(values)
        return {"accepted": True}

    runtime.supervisor.invoke_command = invoke_command  # type: ignore[method-assign]
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id="legacy-command",
                room_name="command-room",
                status="running",
                source_type="browser-tab",
                source_name="Shared tab",
                language="en-US",
            )
        )
        db_session.flush()
        media = MediaRepository(db_session).ensure_legacy_session_bridge(
            "legacy-command"
        )
        repository = PluginRepository(db_session)
        repository.record_package(
            plugin_id="com.example.viewer",
            version="1.0.0",
            content_digest="sha256:" + "a" * 64,
            manifest_hash="sha256:" + "b" * 64,
            image_digest="sha256:" + "c" * 64,
            signature_status="verified",
            package_path="plugins/viewer",
            publisher_id=None,
            manifest_json={
                "id": "com.example.viewer",
                "version": "1.0.0",
                "commands": ["generate_final"],
            },
        )
        repository.ensure_installation(
            plugin_id="com.example.viewer",
            preferred_version="1.0.0",
            status="enabled",
        )
        repository.bind_session(
            plugin_id="com.example.viewer",
            version="1.0.0",
            media_session_id=media.id,
            session_scope="scope-owned-command",
        )
        repository.publish_view(
            plugin_id="com.example.viewer",
            version="1.0.0",
            media_session_id=media.id,
            surface="panel",
            view_id="main",
            view_version=1,
            view_json={
                "actions": [{"id": "run", "command": "generate_final"}]
            },
        )
        db_session.commit()

    async def execute_twice():
        values = {
            "plugin_id": "com.example.viewer",
            "plugin_version": "1.0.0",
            "session_scope": "scope-owned-command",
            "surface": "panel",
            "view_id": "main",
            "expected_view_version": 1,
            "action_id": "run",
            "values": {},
        }
        first = await runtime.execute_plugin_command(media.id, **values)
        second = await runtime.execute_plugin_command(media.id, **values)
        return first, second

    try:
        first, second = __import__("asyncio").run(execute_twice())
        assert first["command_id"] != second["command_id"]
        assert calls[0]["command_id"] == first["command_id"]
        assert calls[1]["command_id"] == second["command_id"]
    finally:
        database.dispose()
