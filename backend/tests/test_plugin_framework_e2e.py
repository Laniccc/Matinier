from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import threading
import time
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.assistant.context import ContextBuilder
from app.main import create_app
from app.persistence.database import Database
from app.persistence.models import SegmentRecord, SessionRecord
from app.plugins.bootstrap import BuiltinPluginBuildDisabledError, PluginHostRuntime
from app.plugins.container_runtime import ImportedImage, PluginContainerSpec, PluginIdentity
from app.plugins.permissions import PermissionDeniedError
from app.plugins.repository import PluginRepository
from app.plugins.signing import assets_digest, canonical_json_bytes, signed_material
from app.settings import Settings


PLUGIN_ID = "com.example.diagnostic"
COURSE_PLUGIN_ID = "com.matinier.course-organizer"


class ScriptedPeer:
    def __init__(self, runtime: "FakeContainerRuntime", version: str) -> None:
        self.runtime = runtime
        self.version = version
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.notifications: list[tuple[str, dict[str, object]]] = []
        self.handlers = {}
        self.closed = False

    @property
    def pending_request_count(self) -> int:
        return 0

    def register_handler(self, method, handler, **_kwargs) -> None:
        self.handlers[method] = handler

    async def invoke_host(self, params: dict[str, object]) -> object:
        return await self.handlers["capability.invoke"](params)

    async def start(self) -> None:
        return None

    async def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float,
    ) -> object:
        del timeout
        self.calls.append((method, params))
        if method == "plugin.initialize":
            return {
                "plugin_id": PLUGIN_ID,
                "version": self.version,
                "protocol_version": "1.0",
                "host_api": "1.0.0",
            }
        if method == "session.open":
            return {"opened": True}
        if method == "event.batch":
            events = params["events"]
            assert isinstance(events, list)
            return {"acknowledged_sequence": max(item["sequence"] for item in events)}
        if method == "plugin.migrate_state":
            if self.version in self.runtime.fail_migration_versions:
                raise RuntimeError("simulated migration failure")
            return {"items": params["items"]}
        if method == "plugin.heartbeat":
            return {"ok": True}
        return {"accepted": True}

    async def notify(self, method: str, params: dict[str, object]) -> None:
        self.notifications.append((method, params))

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeProcess:
    container_id: str
    peer: ScriptedPeer

    def __post_init__(self) -> None:
        import asyncio

        self._exit: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def wait(self) -> int:
        return await self._exit

    def crash(self, exit_code: int) -> None:
        if not self._exit.done():
            self._exit.set_result(exit_code)


class FakeContainerRuntime:
    def __init__(self) -> None:
        self.processes: dict[tuple[str, str], list[FakeProcess]] = defaultdict(list)
        self.fail_migration_versions: set[str] = set()

    async def available(self) -> bool:
        return True

    async def import_image(self, image_tar: Path, expected_digest: str) -> ImportedImage:
        assert "sha256:" + hashlib.sha256(image_tar.read_bytes()).hexdigest() == expected_digest
        version = image_tar.read_text("utf-8").split(":", 1)[1]
        return ImportedImage(expected_digest, f"diagnostic:{version}")

    async def start(self, spec: PluginContainerSpec) -> FakeProcess:
        peer = ScriptedPeer(self, spec.version)
        identity = (spec.plugin_id, spec.version)
        process = FakeProcess(
            container_id=f"diagnostic-{spec.version}-{len(self.processes[identity]) + 1}",
            peer=peer,
        )
        self.processes[identity].append(process)
        return process

    async def stop(self, container_id: str, *, timeout: float) -> None:
        del timeout
        for processes in self.processes.values():
            for process in processes:
                if process.container_id == container_id:
                    process.crash(0)


def _settings(
    tmp_path: Path,
    *,
    plugin_builtin_build_enabled: bool = False,
) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        database_url=f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}",
        data_dir=tmp_path / "data",
        plugin_admin_token="admin-secret",
        plugin_builtin_build_enabled=plugin_builtin_build_enabled,
        plugin_builtin_work_dir=tmp_path / "builtin-work",
        plugin_rpc_timeout_seconds=1,
        plugin_shutdown_timeout_seconds=1,
        plugin_crash_loop_max_restarts=3,
    )


class FakeBuiltinPackageService:
    def __init__(self, package_path: Path) -> None:
        self.package_path = package_path
        self.calls: list[str] = []

    async def prepare(self, plugin_id: str) -> Path:
        self.calls.append(plugin_id)
        return self.package_path


def test_builtin_inspection_resolves_allowlist_and_offloads_blocking_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, plugin_builtin_build_enabled=True)
    database = Database(settings.database_url)
    database.create_schema()
    package_path = tmp_path / "prepared.plugin.zip"
    package_path.write_bytes(b"prepared")
    package_service = FakeBuiltinPackageService(package_path)
    runtime = PluginHostRuntime(
        settings,
        database,
        container_runtime=FakeContainerRuntime(),
        builtin_package_service=package_service,
    )
    calling_thread = threading.get_ident()
    cleanup_threads: list[int] = []
    inspection_threads: list[int] = []

    monkeypatch.setattr(
        runtime,
        "_cleanup_expired_authority",
        lambda: cleanup_threads.append(threading.get_ident()),
    )

    def inspect(path: Path) -> dict[str, object]:
        inspection_threads.append(threading.get_ident())
        assert path == package_path
        return {"ticket_id": "builtin-ticket"}

    monkeypatch.setattr(runtime.package_store, "inspect", inspect)

    result = asyncio.run(runtime.inspect_builtin_plugin(COURSE_PLUGIN_ID))

    assert result == {"ticket_id": "builtin-ticket"}
    assert package_service.calls == [COURSE_PLUGIN_ID]
    assert cleanup_threads and cleanup_threads[0] != calling_thread
    assert inspection_threads and inspection_threads[0] != calling_thread

    with pytest.raises(LookupError):
        asyncio.run(runtime.inspect_builtin_plugin("../course-organizer"))
    assert package_service.calls == [COURSE_PLUGIN_ID]
    database.dispose()


def test_unknown_builtin_is_rejected_before_disabled_build_state(tmp_path: Path) -> None:
    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    settings = _settings(disabled_root, plugin_builtin_build_enabled=False)
    database = Database(settings.database_url)
    database.create_schema()
    package_service = FakeBuiltinPackageService(
        disabled_root / "should-not-be-used.plugin.zip"
    )
    runtime = PluginHostRuntime(
        settings,
        database,
        container_runtime=FakeContainerRuntime(),
        builtin_package_service=package_service,
    )

    with pytest.raises(LookupError):
        asyncio.run(runtime.inspect_builtin_plugin("../course-organizer"))
    with pytest.raises(BuiltinPluginBuildDisabledError):
        asyncio.run(runtime.inspect_builtin_plugin(COURSE_PLUGIN_ID))
    assert package_service.calls == []
    database.dispose()


def _seed_session(database: Database, session_id: str) -> None:
    now = dt.datetime.now(dt.UTC)
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id=session_id,
                room_name=f"room-{session_id}",
                status="completed",
                stop_reason="source_ended",
                source_type="browser-tab",
                source_name="Shared tab",
                language="en-US",
                ended_at=now,
            )
        )
        db_session.flush()
        db_session.add(
            SegmentRecord(
                id=str(uuid.uuid4()),
                session_id=session_id,
                segment_id=f"segment-{session_id}",
                track_id="track-1",
                revision=1,
                language="en-US",
                raw_text="The release is approved",
                display_text="The release is approved.",
                audio_start_ms=100,
                audio_end_ms=800,
                confidence=0.98,
                status="final",
                received_at_ms=1_000,
                finalized_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db_session.commit()


def _package(
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    version: str,
) -> Path:
    image = f"image:{version}".encode()
    image_digest = "sha256:" + hashlib.sha256(image).hexdigest()
    manifest = {
        "schema_version": 1,
        "id": PLUGIN_ID,
        "name": "Diagnostic",
        "version": version,
        "publisher": "Example Publisher",
        "host_api": ">=1.0 <2.0",
        "image_digest": image_digest,
        "subscriptions": ["transcript.final"],
        "permissions": ["state.get", "state.put", "ui.publish"],
        "commands": ["refresh"],
        "resources": {"memory_mb": 64, "cpu_count": 0.25, "pids": 16, "tmpfs_mb": 16},
        "ui_schema_version": 1,
        "state_schema_version": 1,
    }
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = assets_digest({})
    envelope = {
        "schema_version": 1,
        "algorithm": "Ed25519",
        "publisher": manifest["publisher"],
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "assets_digest": digest,
        "signature": base64.b64encode(
            private_key.sign(signed_material(manifest, image_digest, digest))
        ).decode("ascii"),
    }
    target = tmp_path / f"diagnostic-{version}.plugin.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", canonical_json_bytes(manifest))
        archive.writestr("image.tar", image)
        archive.writestr("signature.json", canonical_json_bytes(envelope))
    return target


def _install(client: TestClient, package: Path, *, trust: bool) -> dict[str, object]:
    headers = {
        "Content-Type": "application/vnd.matinier.plugin+zip",
        "X-Plugin-Admin-Token": "admin-secret",
    }
    inspected = client.post(
        "/api/plugins/packages:inspect",
        headers=headers,
        content=package.read_bytes(),
    )
    assert inspected.status_code == 200, inspected.text
    inspection = inspected.json()
    installed = client.post(
        "/api/plugins/installations",
        headers={"X-Plugin-Admin-Token": "admin-secret"},
        json={
            "ticket_id": inspection["ticket_id"],
            "accepted_permissions": inspection["permissions"],
            "trust_publisher": trust,
            "approved_publisher_fingerprint": (
                inspection["publisher_fingerprint"] if trust else None
            ),
        },
    )
    assert installed.status_code == 201, installed.text
    return installed.json()


def test_framework_install_delivery_recovery_update_rollback_and_compatibility(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_schema()
    _seed_session(database, "legacy-one")
    _seed_session(database, "legacy-two")
    container_runtime = FakeContainerRuntime()
    runtime = PluginHostRuntime(
        settings,
        database,
        container_runtime=container_runtime,
        peer_factory=lambda process: process.peer,
    )
    assert {
        "delivery.prepare",
        "delivery.query",
        "document.publish",
        "model.invoke",
    } <= set(runtime.capability_registry._bindings)  # noqa: SLF001
    private_key = Ed25519PrivateKey.generate()
    v1 = _package(tmp_path, private_key, "1.0.0")
    v2 = _package(tmp_path, private_key, "2.0.0")
    v3 = _package(tmp_path, private_key, "3.0.0")
    admin = {"X-Plugin-Admin-Token": "admin-secret"}

    with TestClient(
        create_app(
            settings=settings,
            database=database,
            plugin_host_runtime=runtime,
        )
    ) as client:
        assert _install(client, v1, trust=True)["status"] == "disabled"
        enabled = client.post(f"/api/plugins/{PLUGIN_ID}/enable", headers=admin)
        assert enabled.status_code == 200
        assert enabled.json()["runtime_status"] == "ready"

        bridge = client.get("/api/sessions/legacy-one/media-session")
        assert bridge.status_code == 200
        media_one = bridge.json()["media_session_id"]
        peer_v1 = container_runtime.processes[(PLUGIN_ID, "1.0.0")][-1].peer
        binding = runtime.supervisor.binding(PluginIdentity(PLUGIN_ID, "1.0.0"), media_one)
        event_batch = [call for call in peer_v1.calls if call[0] == "event.batch"]
        assert event_batch and event_batch[-1][1]["events"][0]["sequence"] == 1

        client.portal.call(
            peer_v1.invoke_host,
            {
                "capability": "ui.publish",
                "session_scope": binding.scope,
                "input": {
                    "surface": "panel",
                    "view_id": "main",
                    "view_version": 1,
                    "view": {"id": "ready", "type": "text", "text": "Ready"},
                    "actions": [],
                },
            },
        )
        assert client.get(f"/api/media-sessions/{media_one}/plugin-views").json()[0][
            "view"
        ]["root"]["text"] == "Ready"

        with database.session() as db_session:
            stored = PluginRepository(db_session).get_binding_for_identity(
                plugin_id=PLUGIN_ID,
                version="1.0.0",
                media_session_id=media_one,
            )
            assert stored is not None
            acknowledged = stored.last_acknowledged_sequence
            assert acknowledged == runtime.list_media_events(
                media_one,
                after_sequence=0,
                limit=100,
            )[-1]["sequence"]

        first = container_runtime.processes[(PLUGIN_ID, "1.0.0")][-1]
        first.crash(17)
        deadline = time.monotonic() + 3
        while len(container_runtime.processes[(PLUGIN_ID, "1.0.0")]) < 2:
            if time.monotonic() >= deadline:
                raise AssertionError("plugin did not recover")
            time.sleep(0.01)
        recovered_peer = container_runtime.processes[(PLUGIN_ID, "1.0.0")][-1].peer
        reopened = [call for call in recovered_peer.calls if call[0] == "session.open"]
        assert reopened[-1][1]["after_sequence"] == acknowledged

        client.portal.call(
            recovered_peer.invoke_host,
            {
                "capability": "state.put",
                "session_scope": runtime.supervisor.binding(
                    PluginIdentity(PLUGIN_ID, "1.0.0"), media_one
                ).scope,
                "input": {
                    "key": "cursor",
                    "value": {"sequence": acknowledged},
                    "expected_version": 0,
                },
            },
        )

        assert _install(client, v2, trust=False)["preferred_version"] == "2.0.0"
        assert runtime.supervisor.binding(
            PluginIdentity(PLUGIN_ID, "1.0.0"), media_one
        ).media_session_id == media_one
        second_bridge = client.get("/api/sessions/legacy-two/media-session")
        media_two = second_bridge.json()["media_session_id"]
        assert runtime.supervisor.binding(
            PluginIdentity(PLUGIN_ID, "2.0.0"), media_two
        ).media_session_id == media_two

        container_runtime.fail_migration_versions.add("3.0.0")
        headers = {
            "Content-Type": "application/vnd.matinier.plugin+zip",
            **admin,
        }
        inspection = client.post(
            "/api/plugins/packages:inspect", headers=headers, content=v3.read_bytes()
        ).json()
        rejected = client.post(
            "/api/plugins/installations",
            headers=admin,
            json={
                "ticket_id": inspection["ticket_id"],
                "accepted_permissions": inspection["permissions"],
                "trust_publisher": False,
                "approved_publisher_fingerprint": None,
            },
        )
        assert rejected.status_code == 409
        assert client.get(f"/api/plugins/{PLUGIN_ID}").json()["preferred_version"] == "2.0.0"

        with database.session() as db_session:
            repository = PluginRepository(db_session)
            repository.revoke_base_permission(
                plugin_id=PLUGIN_ID,
                version="1.0.0",
                permission="state.get",
            )
            db_session.commit()
        with pytest.raises(PermissionDeniedError):
            client.portal.call(
                recovered_peer.invoke_host,
                {
                    "capability": "state.get",
                    "session_scope": runtime.supervisor.binding(
                        PluginIdentity(PLUGIN_ID, "1.0.0"), media_one
                    ).scope,
                    "input": {"key": "cursor"},
                },
            )

        with database.session() as db_session:
            snapshot = client.portal.call(
                lambda: ContextBuilder(db_session).build(
                    session_id="legacy-one",
                    goal="What was approved?",
                    persist=False,
                )
            )
            assert any(
                message.message_kind == "caption"
                and "release is approved" in message.display_text.casefold()
                for message in snapshot.evidence_messages
            )

    database.dispose()
