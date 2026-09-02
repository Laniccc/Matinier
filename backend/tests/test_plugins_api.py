from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.persistence.database import Database
from app.plugins.bootstrap import BuiltinPluginBuildDisabledError
from app.settings import Settings


@dataclass
class FakePluginHostRuntime:
    package_bytes: bytes | None = None
    started: bool = False
    stopped: bool = False
    failure: bool = False
    builtin_failure: str | None = None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def health_snapshot(self) -> dict[str, object]:
        return {
            "framework_enabled": True,
            "container_runtime_available": False,
            "installed_count": 1,
            "enabled_count": 0,
            "ready_count": 0,
            "quarantined_count": 0,
            "media_projector_status": "running",
            "media_projector_lag": 0,
            "rpc_pending_count": 0,
        }

    def inspect_package(self, package_path: Path) -> dict[str, object]:
        self.package_bytes = package_path.read_bytes()
        return {
            "ticket_id": "ticket-1",
            "plugin_id": "com.example.viewer",
            "version": "1.0.0",
            "name": "Viewer",
            "publisher_name": "Example",
            "publisher_fingerprint": "sha256:" + "1" * 64,
            "publisher_trusted": False,
            "signature_status": "verified",
            "permissions": ["ui.publish"],
            "content_digest": "sha256:" + "2" * 64,
            "manifest_hash": "sha256:" + "3" * 64,
            "expires_at": "2026-08-28T12:00:00Z",
        }

    def list_builtin_plugins(self) -> list[dict[str, object]]:
        return [
            {
                "id": "com.matinier.course-organizer",
                "name": "Course Organizer",
                "description": "Course notes and knowledge organization",
                "version": "1.0.0",
                "dynamic_build_available": self.builtin_failure != "disabled",
                "source_dir": r"E:\private\course-organizer",
                "signing_key_path": r"C:\private\signing-key.pem",
                "image_command": "docker build secret",
            }
        ]

    async def inspect_builtin_plugin(self, plugin_id: str) -> dict[str, object]:
        if plugin_id != "com.matinier.course-organizer":
            raise LookupError("unknown built-in source")
        if self.builtin_failure == "disabled":
            raise BuiltinPluginBuildDisabledError("dynamic build is disabled")
        if self.builtin_failure == "internal":
            raise RuntimeError(r"failed under C:\private\signing-key.pem")
        return {
            "ticket_id": "builtin-ticket-1",
            "plugin_id": plugin_id,
            "version": "1.0.0",
            "name": "Course Organizer",
            "publisher_name": "Matinier",
            "publisher_fingerprint": "sha256:" + "4" * 64,
            "publisher_trusted": False,
            "signature_status": "verified",
            "permissions": [
                "delivery.prepare",
                "delivery.query",
                "document.publish",
                "model.invoke",
                "state.get",
                "state.put",
                "ui.publish",
            ],
            "content_digest": "sha256:" + "5" * 64,
            "manifest_hash": "sha256:" + "6" * 64,
            "expires_at": "2026-08-28T12:00:00Z",
        }

    async def confirm_install(self, **values: object) -> dict[str, object]:
        assert values["ticket_id"] == "ticket-1"
        assert values["accepted_permissions"] == ("ui.publish",)
        return self.detail("com.example.viewer")

    def list_plugins(self) -> list[dict[str, object]]:
        return [self.detail("com.example.viewer")]

    def detail(self, plugin_id: str) -> dict[str, object]:
        if self.failure:
            raise RuntimeError("database password=must-not-leak")
        if plugin_id != "com.example.viewer":
            raise LookupError("not found")
        return {
            "plugin_id": plugin_id,
            "name": "Viewer",
            "status": "disabled",
            "preferred_version": "1.0.0",
            "versions": ["1.0.0"],
            "permissions": ["ui.publish"],
            "runtime_status": "installed",
            "quarantine_reason": None,
        }

    async def enable_plugin(self, plugin_id: str) -> dict[str, object]:
        result = self.detail(plugin_id)
        return {**result, "status": "enabled", "runtime_status": "ready"}

    async def disable_plugin(self, plugin_id: str) -> dict[str, object]:
        return self.detail(plugin_id)

    def create_grant(self, plugin_id: str, **_values: object) -> dict[str, object]:
        self.detail(plugin_id)
        return {"grant_id": "grant-1", "status": "active"}

    async def uninstall_plugin(self, plugin_id: str) -> None:
        self.detail(plugin_id)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        database_url="sqlite://",
        data_dir=tmp_path / "data",
        plugin_admin_token="admin-secret",
        plugin_package_max_compressed_bytes=16,
        plugin_package_max_uncompressed_bytes=32,
    )


def test_plugin_management_is_two_phase_streamed_and_admin_protected(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    runtime = FakePluginHostRuntime()
    app = create_app(
        settings=settings(tmp_path),
        database=database,
        plugin_host_runtime=runtime,
    )
    headers = {"X-Plugin-Admin-Token": "admin-secret"}
    with TestClient(app) as client:
        missing = client.post(
            "/api/plugins/packages:inspect",
            content=b"zip",
            headers={"Content-Type": "application/vnd.matinier.plugin+zip"},
        )
        assert missing.status_code == 401
        wrong = client.post(
            "/api/plugins/packages:inspect",
            content=b"zip",
            headers={
                "Content-Type": "application/vnd.matinier.plugin+zip",
                "X-Plugin-Admin-Token": "wrong-secret",
            },
        )
        assert wrong.status_code == 403

        inspected = client.post(
            "/api/plugins/packages:inspect",
            content=b"raw-plugin-zip",
            headers={
                **headers,
                "Content-Type": "application/vnd.matinier.plugin+zip",
            },
        )
        assert inspected.status_code == 200
        assert inspected.json()["ticket_id"] == "ticket-1"
        assert runtime.package_bytes == b"raw-plugin-zip"

        oversized = client.post(
            "/api/plugins/packages:inspect",
            content=b"x" * 17,
            headers={
                **headers,
                "Content-Type": "application/vnd.matinier.plugin+zip",
            },
        )
        assert oversized.status_code == 413
        wrong_media = client.post(
            "/api/plugins/packages:inspect",
            content=b"zip",
            headers={**headers, "Content-Type": "application/json"},
        )
        assert wrong_media.status_code == 415

        installed = client.post(
            "/api/plugins/installations",
            headers=headers,
            json={
                "ticket_id": "ticket-1",
                "accepted_permissions": ["ui.publish"],
                "trust_publisher": True,
                "approved_publisher_fingerprint": "sha256:" + "1" * 64,
            },
        )
        assert installed.status_code == 201
        assert installed.json()["status"] == "disabled"

        assert client.get("/api/plugins").status_code == 200
        assert client.get("/api/plugins/com.example.viewer").status_code == 200
        enabled = client.post(
            "/api/plugins/com.example.viewer/enable", headers=headers
        )
        assert enabled.json()["runtime_status"] == "ready"
        assert client.post(
            "/api/plugins/com.example.viewer/disable", headers=headers
        ).status_code == 200
        grant = client.post(
            "/api/plugins/com.example.viewer/grants",
            headers=headers,
            json={
                "media_session_id": None,
                "capability": "network.fetch",
                "effect": "network",
                "scope": {"destinations": ["api.example.com"]},
                "ttl_seconds": 900,
            },
        )
        assert grant.json() == {"grant_id": "grant-1", "status": "active"}
        assert client.delete(
            "/api/plugins/com.example.viewer", headers=headers
        ).status_code == 204

    assert runtime.started is True
    assert runtime.stopped is True


def test_plugin_api_returns_safe_errors(tmp_path: Path) -> None:
    database = Database("sqlite://")
    database.create_schema()
    runtime = FakePluginHostRuntime(failure=True)
    with TestClient(
        create_app(
            settings=settings(tmp_path),
            database=database,
            plugin_host_runtime=runtime,
        )
    ) as client:
        response = client.get("/api/plugins/com.example.viewer")
        assert response.status_code == 500
        assert response.json() == {"detail": "Plugin operation failed"}
        assert "password" not in response.text


def test_builtin_catalog_is_public_and_response_is_explicitly_whitelisted(
    tmp_path: Path,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    with TestClient(
        create_app(
            settings=settings(tmp_path),
            database=database,
            plugin_host_runtime=FakePluginHostRuntime(),
        )
    ) as client:
        response = client.get("/api/plugins/builtins")

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": "com.matinier.course-organizer",
                "name": "Course Organizer",
                "description": "Course notes and knowledge organization",
                "version": "1.0.0",
                "dynamic_build_available": True,
            }
        ]
        serialized = response.text.casefold()
        assert "source_dir" not in serialized
        assert "signing" not in serialized
        assert "docker" not in serialized

        operation = client.get("/openapi.json").json()["paths"][
            "/api/plugins/builtins/{plugin_id}/packages:inspect"
        ]["post"]
        assert "requestBody" not in operation


def test_builtin_inspection_is_admin_protected_and_uses_existing_shape(
    tmp_path: Path,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    runtime = FakePluginHostRuntime()
    with TestClient(
        create_app(
            settings=settings(tmp_path),
            database=database,
            plugin_host_runtime=runtime,
        )
    ) as client:
        path = "/api/plugins/builtins/com.matinier.course-organizer/packages:inspect"
        assert client.post(path).status_code == 401
        assert client.post(
            path,
            headers={"X-Plugin-Admin-Token": "wrong-secret"},
        ).status_code == 403

        response = client.post(
            path,
            headers={"X-Plugin-Admin-Token": "admin-secret"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ticket_id"] == "builtin-ticket-1"
        assert body["permissions"] == [
            "delivery.prepare",
            "delivery.query",
            "document.publish",
            "model.invoke",
            "state.get",
            "state.put",
            "ui.publish",
        ]


def test_builtin_inspection_maps_public_errors_without_leaking_paths(
    tmp_path: Path,
) -> None:
    admin = {"X-Plugin-Admin-Token": "admin-secret"}

    for runtime, plugin_id, expected_status, expected_detail in (
        (
            FakePluginHostRuntime(builtin_failure="disabled"),
            "com.matinier.course-organizer",
            409,
            "Built-in plugin build is disabled",
        ),
        (FakePluginHostRuntime(), "unknown", 404, "Built-in plugin not found"),
        (
            FakePluginHostRuntime(builtin_failure="internal"),
            "com.matinier.course-organizer",
            500,
            "Built-in plugin inspection failed",
        ),
    ):
        database = Database("sqlite://")
        database.create_schema()
        with TestClient(
            create_app(
                settings=settings(tmp_path),
                database=database,
                plugin_host_runtime=runtime,
            )
        ) as client:
            response = client.post(
                f"/api/plugins/builtins/{plugin_id}/packages:inspect",
                headers=admin,
            )
        assert response.status_code == expected_status
        assert response.json() == {"detail": expected_detail}
        assert "private" not in response.text.casefold()
        assert "signing-key" not in response.text.casefold()
