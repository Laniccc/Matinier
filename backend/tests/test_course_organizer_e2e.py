from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import sys
import time
import uuid
import zipfile
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.assistant.context import ContextBuilder
from app.main import create_app
from app.media.projector import MediaEventProjector, ProjectorConfig
from app.persistence.database import Database
from app.persistence.models import (
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
)
from app.plugins.bootstrap import PluginHostRuntime
from app.plugins.container_runtime import ImportedImage, PluginContainerSpec, PluginIdentity
from app.plugins.repository import PluginRepository
from app.plugins.signing import assets_digest, canonical_json_bytes, signed_material
from app.settings import Settings
from app.text_processing.provider import (
    DeepSeekRequestError,
    StructuredCompletionRequest,
    StructuredCompletionResult,
)


ROOT = Path(__file__).resolve().parents[2]
COURSE_SOURCE = ROOT / "plugin-sdk" / "examples" / "course-organizer"
SDK_SOURCE = ROOT / "plugin-sdk" / "python"
for source in (COURSE_SOURCE, SDK_SOURCE):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from course_organizer.realtime import RealtimeConfig  # noqa: E402
from course_organizer.session import CourseSession  # noqa: E402


PLUGIN_ID = "com.matinier.course-organizer"
PLUGIN_VERSION = "1.0.3"
DIAGNOSTIC_PLUGIN_ID = "com.example.diagnostic"
ADMIN_HEADERS = {"X-Plugin-Admin-Token": "admin-secret"}


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


class DeterministicCourseProvider:
    provider_name = "fake-course"
    model = "course-fixture-v1"

    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.invalid_map_responses = 1
        self.unavailable = False
        self.block_final = False
        self.blocked_final_calls = 0
        self.final_gate = asyncio.Event()
        self.final_gate.set()

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        payload = dict(request.input_payload)
        task = str(payload.get("task"))
        self.tasks.append(task)
        if self.unavailable:
            raise DeepSeekRequestError("fixture provider unavailable", retryable=True)
        if task == "course.final.map" and self.block_final:
            self.blocked_final_calls += 1
            await self.final_gate.wait()

        items = payload.get("items", [])
        if not isinstance(items, list):
            raise AssertionError("course fixture expected an item list")
        if task == "course.realtime_notes":
            output = {
                "notes": [
                    {
                        "id": f"note-{item['item_id']}",
                        "note_type": item["classification"],
                        "title": f"知识坐标 {item['start_ms']}",
                        "body": item["text"],
                        "evidence_item_ids": [item["item_id"]],
                        "confidence_status": (
                            "needs_confirmation"
                            if item["confirmation_status"] == "needs_confirmation"
                            else "confirmed"
                        ),
                        "language": payload["output_language"],
                        "related_note_ids": [],
                    }
                    for item in items
                    if item["classification"]
                    in {"knowledge_candidate", "example", "needs_confirmation"}
                ]
            }
        elif task == "course.localize_notes":
            output = {
                "notes": [
                    {
                        "id": item["id"],
                        "title": f"[{payload['target_language']}] {item['title']}",
                        "body": f"[{payload['target_language']}] {item['body']}",
                    }
                    for item in items
                ]
            }
        elif task == "course.final.map" and self.invalid_map_responses:
            self.invalid_map_responses -= 1
            output = {"items": [{"id": "invalid-first-response"}]}
        elif task in {"course.final.map", "course.final.map_repair"}:
            output = {
                "items": [
                    {
                        "id": f"knowledge-{index}-{item['item_id']}",
                        "category": (
                            "案例"
                            if "example" in str(item["text"]).casefold()
                            or "例如" in str(item["text"])
                            else "核心概念"
                        ),
                        "topic_path": ["课程", "梯度"],
                        "title": f"知识点 {index + 1}",
                        "statement": item["text"],
                        "explanation": "该条目由冻结课程字幕证据整理。",
                        "evidence_item_ids": [item["item_id"]],
                        "related_item_ids": [],
                        "confirmation_status": "confirmed",
                    }
                    for index, item in enumerate(items)
                ]
            }
        elif task in {"course.final.reduce", "course.final.reduce_repair"}:
            output = {"items": items}
        else:
            raise AssertionError(f"unexpected course model task: {task}")
        return StructuredCompletionResult(
            content=json.dumps(output, ensure_ascii=False),
            finish_reason="stop",
            output_tokens=32,
        )


class CourseCapabilityClient:
    def __init__(self, peer: "CoursePeer") -> None:
        self.peer = peer

    async def capability(
        self,
        *,
        name: str,
        session_scope: str,
        input_value: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        result = await self.peer.handlers["capability.invoke"](
            {
                "capability": name,
                "session_scope": session_scope,
                "input": input_value,
                "idempotency_key": idempotency_key,
            }
        )
        if not isinstance(result, dict):
            raise TypeError("Host capability response must be an object")
        return result


class CoursePeer:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.sessions: dict[str, CourseSession] = {}
        self.closed = False
        self.capabilities = CourseCapabilityClient(self)

    @property
    def pending_request_count(self) -> int:
        return 0

    def register_handler(self, method, handler, **_kwargs) -> None:
        self.handlers[method] = handler

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
        if method == "plugin.initialize":
            return {
                "plugin_id": PLUGIN_ID,
                "version": PLUGIN_VERSION,
                "protocol_version": "1.0",
                "host_api": "1.0.0",
            }
        if method == "session.open":
            scope = str(params["session_scope"])
            session = CourseSession(
                self.capabilities,
                sleep=_no_sleep,
                config=RealtimeConfig(retry_delays_seconds=(1,)),
                final_retry_delays=(1,),
            )
            self.sessions[scope] = session
            return await session.open(params)
        if method == "event.batch":
            return await self.sessions[str(params["session_scope"])].event_batch(params)
        if method == "command.execute":
            return await self.sessions[str(params["session_scope"])].command(params)
        if method == "plugin.heartbeat":
            return {"ok": True}
        if method == "plugin.migrate_state":
            return {"items": params["items"]}
        raise AssertionError(f"unexpected plugin request: {method}")

    async def notify(self, method: str, params: dict[str, object]) -> None:
        if method == "session.close":
            session = self.sessions.pop(str(params["session_scope"]), None)
            if session is not None:
                await session.close()
        elif method == "plugin.shutdown":
            await self.aclose()

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await asyncio.gather(
            *(session.close() for session in self.sessions.values()),
            return_exceptions=True,
        )
        self.sessions.clear()


class DiagnosticPeer:
    def __init__(self, version: str) -> None:
        self.version = version
        self.handlers: dict[str, object] = {}
        self.commands: list[dict[str, object]] = []
        self.closed = False

    @property
    def pending_request_count(self) -> int:
        return 0

    def register_handler(self, method, handler, **_kwargs) -> None:
        self.handlers[method] = handler

    async def invoke_host(self, params: dict[str, object]) -> object:
        handler = self.handlers["capability.invoke"]
        return await handler(params)  # type: ignore[operator]

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
        if method == "plugin.initialize":
            return {
                "plugin_id": DIAGNOSTIC_PLUGIN_ID,
                "version": self.version,
                "protocol_version": "1.0",
                "host_api": "1.0.0",
            }
        if method == "session.open":
            return {"opened": True}
        if method == "event.batch":
            events = params["events"]
            assert isinstance(events, list) and events
            return {
                "acknowledged_sequence": max(
                    int(item["sequence"])
                    for item in events
                    if isinstance(item, dict)
                )
            }
        if method == "command.execute":
            self.commands.append(dict(params))
            return {"accepted": True}
        if method == "plugin.heartbeat":
            return {"ok": True}
        if method == "plugin.migrate_state":
            return {"items": params["items"]}
        raise AssertionError(f"unexpected diagnostic request: {method}")

    async def notify(self, method: str, params: dict[str, object]) -> None:
        del params
        if method == "plugin.shutdown":
            await self.aclose()

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class CourseProcess:
    container_id: str
    peer: CoursePeer

    def __post_init__(self) -> None:
        self._exit: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def wait(self) -> int:
        return await self._exit

    def crash(self, exit_code: int = 17) -> None:
        if not self._exit.done():
            self._exit.set_result(exit_code)


class FakeCourseContainerRuntime:
    def __init__(self) -> None:
        self.processes: dict[tuple[str, str], list[CourseProcess]] = defaultdict(list)

    async def available(self) -> bool:
        return True

    async def import_image(self, image_tar: Path, expected_digest: str) -> ImportedImage:
        assert "sha256:" + hashlib.sha256(image_tar.read_bytes()).hexdigest() == expected_digest
        return ImportedImage(expected_digest, "course-organizer:fixture")

    async def start(self, spec: PluginContainerSpec) -> CourseProcess:
        identity = (spec.plugin_id, spec.version)
        process = CourseProcess(
            container_id=f"course-{len(self.processes[identity]) + 1}",
            peer=CoursePeer(),
        )
        self.processes[identity].append(process)
        return process

    async def stop(self, container_id: str, *, timeout: float) -> None:
        del timeout
        for processes in self.processes.values():
            for process in processes:
                if process.container_id == container_id:
                    process.crash(0)


class FakeMultiPluginContainerRuntime(FakeCourseContainerRuntime):
    async def start(self, spec: PluginContainerSpec) -> CourseProcess:
        if spec.plugin_id == PLUGIN_ID:
            return await super().start(spec)
        if spec.plugin_id != DIAGNOSTIC_PLUGIN_ID:
            raise AssertionError(f"unexpected plugin identity: {spec.plugin_id}")
        identity = (spec.plugin_id, spec.version)
        process = CourseProcess(
            container_id=f"diagnostic-{len(self.processes[identity]) + 1}",
            peer=DiagnosticPeer(spec.version),  # type: ignore[arg-type]
        )
        self.processes[identity].append(process)
        return process


class ManualMediaProjector:
    def __init__(self, database: Database) -> None:
        self.delegate = MediaEventProjector(
            database,
            config=ProjectorConfig(poll_interval_ms=60_000, batch_size=100),
        )
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def request_catch_up(self, legacy_session_id: str):
        return await self.delegate.request_catch_up(legacy_session_id)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        database_url=f"sqlite:///{(tmp_path / 'course-e2e.db').as_posix()}",
        data_dir=tmp_path / "data",
        plugin_admin_token="admin-secret",
        plugin_rpc_timeout_seconds=2,
        plugin_shutdown_timeout_seconds=1,
        plugin_crash_loop_max_restarts=3,
    )


def _seed_session(database: Database, session_id: str, *, captions: bool = True) -> None:
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id=session_id,
                room_name=f"room-{session_id}",
                status="active",
                source_type="browser-tab",
                source_name="Course tab",
                language="en-US",
                target_language="zh-CN",
                translation_status="running",
            )
        )
        db_session.commit()
    if not captions:
        return
    _add_caption_pair(
        database,
        session_id=session_id,
        index=1,
        start_ms=0,
        source="Gradient is defined as the vector of partial derivatives.",
        translation="梯度定义为偏导数组成的向量。",
    )
    _add_caption_pair(
        database,
        session_id=session_id,
        index=2,
        start_ms=61_000,
        source="For example, gradient descent updates parameters opposite the gradient.",
        translation="例如，梯度下降沿梯度反方向更新参数。",
    )


def _add_caption_pair(
    database: Database,
    *,
    session_id: str,
    index: int,
    start_ms: int,
    source: str,
    translation: str,
) -> None:
    now = dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=index)
    segment_id = f"course-segment-{index}"
    with database.session() as db_session:
        db_session.add_all(
            [
                SegmentRecord(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    segment_id=segment_id,
                    track_id="track-course",
                    revision=1,
                    language="en-US",
                    raw_text=source,
                    display_text=source,
                    audio_start_ms=start_ms,
                    audio_end_ms=start_ms + 1_500,
                    confidence=0.98,
                    status="final",
                    received_at_ms=start_ms + 1_500,
                    finalized_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                TranslationSegmentRecord(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    segment_id=f"course-translation-{index}",
                    revision=1,
                    source_language="en-US",
                    target_language="zh-CN",
                    text=translation,
                    audio_start_ms=start_ms,
                    audio_end_ms=start_ms + 1_500,
                    source_segment_ids=[segment_id],
                    status="final",
                    received_at_ms=start_ms + 1_600,
                    finalized_at=now,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        db_session.commit()


def _course_package(tmp_path: Path, private_key: Ed25519PrivateKey) -> Path:
    image = b"course-organizer-fixture-image"
    image_digest = "sha256:" + hashlib.sha256(image).hexdigest()
    manifest = {
        "schema_version": 1,
        "id": PLUGIN_ID,
        "name": "Course Content Organizer",
        "version": PLUGIN_VERSION,
        "publisher": "Matinier Development",
        "host_api": ">=1.0 <2.0",
        "image_digest": image_digest,
        "subscriptions": [
            "session.cancelled",
            "session.completed",
            "session.failed",
            "transcript.final",
            "translation.final",
        ],
        "permissions": [
            "delivery.prepare",
            "delivery.query",
            "document.publish",
            "model.invoke",
            "state.get",
            "state.put",
            "ui.publish",
        ],
        "commands": [
            "generate_final",
            "retry_final",
            "select_version",
            "set_language",
        ],
        "resources": {"memory_mb": 128, "cpu_count": 0.5, "pids": 32, "tmpfs_mb": 32},
        "ui_schema_version": 1,
        "state_schema_version": 1,
    }
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    asset_hash = assets_digest({})
    envelope = {
        "schema_version": 1,
        "algorithm": "Ed25519",
        "publisher": manifest["publisher"],
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "assets_digest": asset_hash,
        "signature": base64.b64encode(
            private_key.sign(signed_material(manifest, image_digest, asset_hash))
        ).decode("ascii"),
    }
    target = tmp_path / "course-organizer.plugin.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", canonical_json_bytes(manifest))
        archive.writestr("image.tar", image)
        archive.writestr("signature.json", canonical_json_bytes(envelope))
    return target


def _diagnostic_package(tmp_path: Path, private_key: Ed25519PrivateKey) -> Path:
    image = b"diagnostic-fixture-image"
    image_digest = "sha256:" + hashlib.sha256(image).hexdigest()
    manifest = {
        "schema_version": 1,
        "id": DIAGNOSTIC_PLUGIN_ID,
        "name": "Diagnostic",
        "version": PLUGIN_VERSION,
        "publisher": "Example Diagnostics",
        "host_api": ">=1.0 <2.0",
        "image_digest": image_digest,
        "subscriptions": ["transcript.final"],
        "permissions": ["ui.publish"],
        "commands": ["refresh"],
        "resources": {
            "memory_mb": 64,
            "cpu_count": 0.25,
            "pids": 16,
            "tmpfs_mb": 16,
        },
        "ui_schema_version": 1,
        "state_schema_version": 1,
    }
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    asset_hash = assets_digest({})
    envelope = {
        "schema_version": 1,
        "algorithm": "Ed25519",
        "publisher": manifest["publisher"],
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "assets_digest": asset_hash,
        "signature": base64.b64encode(
            private_key.sign(signed_material(manifest, image_digest, asset_hash))
        ).decode("ascii"),
    }
    target = tmp_path / "diagnostic.plugin.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", canonical_json_bytes(manifest))
        archive.writestr("image.tar", image)
        archive.writestr("signature.json", canonical_json_bytes(envelope))
    return target


def _install_and_enable(
    client: TestClient,
    package: Path,
    *,
    plugin_id: str = PLUGIN_ID,
) -> None:
    inspected = client.post(
        "/api/plugins/packages:inspect",
        headers={
            **ADMIN_HEADERS,
            "Content-Type": "application/vnd.matinier.plugin+zip",
        },
        content=package.read_bytes(),
    )
    assert inspected.status_code == 200, inspected.text
    inspection = inspected.json()
    installed = client.post(
        "/api/plugins/installations",
        headers=ADMIN_HEADERS,
        json={
            "ticket_id": inspection["ticket_id"],
            "accepted_permissions": inspection["permissions"],
            "trust_publisher": True,
            "approved_publisher_fingerprint": inspection["publisher_fingerprint"],
        },
    )
    assert installed.status_code == 201, installed.text
    enabled = client.post(f"/api/plugins/{plugin_id}/enable", headers=ADMIN_HEADERS)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["runtime_status"] == "ready"


def _wait_for(predicate, *, timeout: float = 5.0, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message}")


def _view(client: TestClient, media_session_id: str) -> dict[str, object]:
    response = client.get(f"/api/media-sessions/{media_session_id}/plugin-views")
    assert response.status_code == 200, response.text
    views = response.json()
    assert len(views) == 1
    return views[0]


def _binding_or_none(
    runtime: PluginHostRuntime,
    identity: PluginIdentity,
    media_session_id: str,
):
    try:
        return runtime.supervisor.binding(identity, media_session_id)
    except LookupError:
        return None


def _execute_action(
    client: TestClient,
    media_session_id: str,
    action_id: str,
    values: dict[str, object] | None = None,
):
    view = _view(client, media_session_id)
    response = client.post(
        f"/api/media-sessions/{media_session_id}/plugin-commands",
        json={
            "plugin_id": view["plugin_id"],
            "plugin_version": view["plugin_version"],
            "session_scope": view["session_scope"],
            "surface": view["surface"],
            "view_id": view["view_id"],
            "expected_view_version": view["view_version"],
            "action_id": action_id,
            "values": values or {},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


@pytest.mark.parametrize("restart", [False, True])
def test_course_stream_delivers_new_captions_and_completion_without_reconnecting(
    tmp_path: Path, restart: bool,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_schema()
    session_id = "continuous-course-session"
    _seed_session(database, session_id, captions=False)
    provider = DeterministicCourseProvider()
    containers = FakeCourseContainerRuntime()
    runtime = PluginHostRuntime(
        settings,
        database,
        container_runtime=containers,
        peer_factory=lambda process: process.peer,
        structured_provider=provider,
    )
    package = _course_package(tmp_path, Ed25519PrivateKey.generate())
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(create_app(
            settings=settings, database=database, plugin_host_runtime=runtime,
        )))
        _install_and_enable(client, package)
        # Connect once BEFORE any captions exist. Further requests only read
        # views/documents; reconnecting would conceal a missing delivery pump.
        bridge = client.get(f"/api/sessions/{session_id}/media-session")
        assert bridge.status_code == 200
        media_id = bridge.json()["media_session_id"]
        assert not provider.tasks
        for index, start in ((1, 0), (2, 61_000)):
            _add_caption_pair(
                database, session_id=session_id, index=index, start_ms=start,
                source="Gradient is defined as the vector of partial derivatives.",
                translation="梯度定义为偏导数组成的向量。",
            )
        _wait_for(
            lambda: any(isinstance(item, dict) and "time_ms" in item
                        for item in _walk(_view(client, media_id)["view"])),
            message="realtime notes without reconnecting", timeout=8,
        )
        assert not client.get(f"/api/media-sessions/{media_id}/plugin-documents").json()
        first_calls = provider.tasks.count("course.realtime_notes")
        if restart:
            stack.close()
            containers = FakeCourseContainerRuntime()
            runtime = PluginHostRuntime(
                settings, database, container_runtime=containers,
                peer_factory=lambda process: process.peer, structured_provider=provider,
            )
            client = stack.enter_context(TestClient(create_app(
                settings=settings, database=database, plugin_host_runtime=runtime,
            )))
            # Restored scopes must resume the stream without another bridge GET.
            assert _view(client, media_id)["plugin_version"] == PLUGIN_VERSION
        for index, start in ((3, 90_000), (4, 151_000)):
            _add_caption_pair(
                database, session_id=session_id, index=index, start_ms=start,
                source="For example, a large learning rate can diverge.",
                translation="例如，过大的学习率会导致发散。",
            )
        _wait_for(
            lambda: provider.tasks.count("course.realtime_notes") > first_calls,
            message="second realtime window without reconnecting", timeout=8,
        )
        with database.session() as session:
            record = session.get(SessionRecord, session_id)
            record.status = "completed"
            record.stop_reason = "source_ended"
            record.ended_at = dt.datetime.now(dt.UTC)
            session.commit()
        complete = _wait_for(
            lambda: next((doc for doc in client.get(
                f"/api/media-sessions/{media_id}/plugin-documents"
            ).json() if doc["completeness"] == "complete"), None),
            message="automatic complete document without reconnecting", timeout=8,
        )
        detail = client.get(f"/api/plugin-documents/{complete['document_id']}").json()
        assert detail["content"]["parts"][0]["items"]
        assert complete["trigger"] == "session_completed"
        with database.session() as session:
            binding = PluginRepository(session).get_binding_for_identity(
                plugin_id=PLUGIN_ID, version=PLUGIN_VERSION, media_session_id=media_id,
            )
            assert binding.last_acknowledged_sequence == 9
            assert binding.last_delivered_sequence == 9


def test_course_plugin_complete_fake_host_workflow(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_schema()
    session_id = "course-session"
    _seed_session(database, session_id)
    provider = DeterministicCourseProvider()
    containers = FakeCourseContainerRuntime()
    runtime = PluginHostRuntime(
        settings,
        database,
        container_runtime=containers,
        media_projector=ManualMediaProjector(database),  # type: ignore[arg-type]
        peer_factory=lambda process: process.peer,
        structured_provider=provider,
    )
    package = _course_package(tmp_path, Ed25519PrivateKey.generate())

    with TestClient(
        create_app(settings=settings, database=database, plugin_host_runtime=runtime)
    ) as client:
        _install_and_enable(client, package)
        bridge = client.get(f"/api/sessions/{session_id}/media-session")
        assert bridge.status_code == 200, bridge.text
        media_session_id = bridge.json()["media_session_id"]
        identity = PluginIdentity(PLUGIN_ID, PLUGIN_VERSION)
        binding = runtime.supervisor.binding(identity, media_session_id)
        first_peer = containers.processes[(PLUGIN_ID, PLUGIN_VERSION)][-1].peer
        first_session = first_peer.sessions[binding.scope]

        _wait_for(
            lambda: first_session.background_done and bool(first_session.state.notes),
            message="initial realtime course notes",
        )
        view = _view(client, media_session_id)
        timeline_items = [
            item
            for item in _walk(view["view"])
            if isinstance(item, dict) and "time_ms" in item and "title" in item
        ]
        assert {item["time_ms"] for item in timeline_items} >= {0, 61_000}
        assert {note.note_type for note in first_session.state.notes} >= {
            "knowledge_candidate",
            "example",
        }

        provider.block_final = True
        provider.final_gate.clear()
        accepted = _execute_action(client, media_session_id, "generate-final")
        assert accepted["accepted"] is True
        _wait_for(lambda: provider.blocked_final_calls == 1, message="blocked final map")
        listed = client.get(
            f"/api/media-sessions/{media_session_id}/plugin-documents"
        )
        assert listed.status_code == 200 and listed.json() == []
        provider.block_final = False
        client.portal.call(provider.final_gate.set)

        interim = _wait_for(
            lambda: next(
                (
                    item
                    for item in client.get(
                        f"/api/media-sessions/{media_session_id}/plugin-documents"
                    ).json()
                    if item["completeness"] == "interim"
                    and item["language"] == "zh-CN"
                ),
                None,
            ),
            message="manual interim document",
        )
        assert interim["document_version"] == 1
        interim_detail = client.get(
            f"/api/plugin-documents/{interim['document_id']}"
        ).json()
        markdown = client.get(
            f"/api/plugin-documents/{interim['document_id']}/export?format=markdown"
        )
        exported_json = client.get(
            f"/api/plugin-documents/{interim['document_id']}/export?format=json"
        )
        assert markdown.text == interim_detail["markdown"]
        assert exported_json.json()["metadata"]["content_hash"] == interim["content_hash"]
        first_package_hash = interim["source_package_hash"]
        assert "course.final.map_repair" in provider.tasks

        realtime_calls = provider.tasks.count("course.realtime_notes")
        _add_caption_pair(
            database,
            session_id=session_id,
            index=3,
            start_ms=90_000,
            source="Because the learning rate controls step size, stability depends on it.",
            translation="因为学习率控制步长，所以稳定性取决于它。",
        )
        _add_caption_pair(
            database,
            session_id=session_id,
            index=4,
            start_ms=151_000,
            source="For example, a very large learning rate can diverge.",
            translation="例如，过大的学习率会导致发散。",
        )
        refreshed = client.get(f"/api/sessions/{session_id}/media-session")
        assert refreshed.status_code == 200, refreshed.text
        _wait_for(
            lambda: provider.tasks.count("course.realtime_notes") > realtime_calls,
            message="continued realtime notes",
        )

        with database.session() as db_session:
            record = db_session.get(SessionRecord, session_id)
            assert record is not None
            record.status = "completed"
            record.stop_reason = "source_ended"
            record.ended_at = dt.datetime.now(dt.UTC)
            db_session.commit()
        terminal_bridge = client.get(f"/api/sessions/{session_id}/media-session")
        assert terminal_bridge.status_code == 200, terminal_bridge.text
        complete = _wait_for(
            lambda: next(
                (
                    item
                    for item in client.get(
                        f"/api/media-sessions/{media_session_id}/plugin-documents"
                    ).json()
                    if item["completeness"] == "complete"
                    and item["language"] == "zh-CN"
                ),
                None,
            ),
            message="automatic terminal document",
        )
        assert complete["document_version"] == 2
        assert complete["source_package_hash"] != first_package_hash
        documents = client.get(
            f"/api/media-sessions/{media_session_id}/plugin-documents"
        ).json()
        assert {(item["document_version"], item["completeness"]) for item in documents} >= {
            (1, "interim"),
            (2, "complete"),
        }

        complete_detail = client.get(
            f"/api/plugin-documents/{complete['document_id']}"
        ).json()
        realtime_part = next(part for part in complete_detail["content"]["parts"]
                             if part["id"] == "realtime-notes")
        assert realtime_part["items"], "realtime notes must survive the Frozen Package evidence join"
        evidence_ids = {item["item_id"] for item in complete_detail["evidence_refs"]}
        knowledge_part = next(
            item for item in complete_detail["content"]["parts"] if item["id"] == "knowledge"
        )
        knowledge_items = [
            item
            for category in knowledge_part["categories"]
            for item in category["items"]
        ]
        assert knowledge_items
        assert all(set(item["evidence_item_ids"]) <= evidence_ids for item in knowledge_items)
        frozen = client.get(f"/api/packages/{complete['source_package_id']}").json()
        package_evidence_ids = {
            item["item_id"]
            for document in frozen["documents"]
            if document["document_kind"] == "evidence_index"
            for item in document["content"]["items"]
        }
        assert evidence_ids <= package_evidence_ids

        _execute_action(
            client,
            media_session_id,
            "set-language",
            {"output_language": "fr-FR"},
        )
        _wait_for(
            lambda: first_session.state.output_language == "fr-FR",
            message="custom document language",
        )
        _execute_action(client, media_session_id, "generate-final")
        french = _wait_for(
            lambda: next(
                (
                    item
                    for item in client.get(
                        f"/api/media-sessions/{media_session_id}/plugin-documents"
                    ).json()
                    if item["language"] == "fr-FR"
                ),
                None,
            ),
            message="independent French document",
        )
        assert french["identity_key"] == "course-notes:fr-FR"
        assert french["document_version"] == 1

        before_restart_count = len(
            client.get(
                f"/api/media-sessions/{media_session_id}/plugin-documents"
            ).json()
        )
        with database.session() as db_session:
            stored = PluginRepository(db_session).get_binding_for_identity(
                plugin_id=PLUGIN_ID,
                version=PLUGIN_VERSION,
                media_session_id=media_session_id,
            )
            assert stored is not None
            acknowledged = stored.last_acknowledged_sequence
        client.portal.call(
            containers.processes[(PLUGIN_ID, PLUGIN_VERSION)][-1].crash
        )
        _wait_for(
            lambda: len(containers.processes[(PLUGIN_ID, PLUGIN_VERSION)]) == 2,
            message="course container restart",
        )
        restored_binding = _wait_for(
            lambda: _binding_or_none(runtime, identity, media_session_id),
            message="restored course binding",
        )
        restored_peer = containers.processes[(PLUGIN_ID, PLUGIN_VERSION)][-1].peer
        restored_session = _wait_for(
            lambda: restored_peer.sessions.get(restored_binding.scope),
            message="restored course session",
        )
        assert restored_session.state.last_sequence == acknowledged
        assert restored_session.state.terminal_job_id is None
        time.sleep(0.05)
        assert len(
            client.get(
                f"/api/media-sessions/{media_session_id}/plugin-documents"
            ).json()
        ) == before_restart_count

        provider.unavailable = True
        _add_caption_pair(
            database,
            session_id=session_id,
            index=5,
            start_ms=220_000,
            source="Gradient clipping is defined as limiting the gradient norm.",
            translation="梯度裁剪定义为限制梯度范数。",
        )
        _add_caption_pair(
            database,
            session_id=session_id,
            index=6,
            start_ms=281_000,
            source="For example, clipping prevents an exploding update.",
            translation="例如，裁剪可以防止更新爆炸。",
        )
        degraded_bridge = client.get(f"/api/sessions/{session_id}/media-session")
        assert degraded_bridge.status_code == 200, degraded_bridge.text
        _wait_for(
            lambda: restored_session.state.status == "degraded"
            and any(
                item.confidence_status == "rule_fallback"
                for item in restored_session.state.notes
            ),
            message="rule-based realtime degradation",
        )
        _execute_action(client, media_session_id, "generate-final")
        _wait_for(
            lambda: restored_session.state.final_status == "waiting_retry",
            message="final generation retry state",
        )
        assert restored_session.state.final_error == "最终整理暂时失败，可稍后重试。"

        with database.session() as db_session:
            snapshot = client.portal.call(
                lambda: ContextBuilder(db_session).build(
                    session_id=session_id,
                    goal="What course concepts were explained?",
                    persist=False,
                )
            )
        caption_text = " ".join(
            message.display_text
            for message in snapshot.evidence_messages
            if message.message_kind == "caption"
        )
        assert "Gradient is defined" in caption_text
        assert "Gradient clipping" in caption_text

    database.dispose()


def test_two_plugins_share_session_without_view_command_or_document_leakage(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    database.create_schema()
    session_id = "multi-plugin-course-session"
    _seed_session(database, session_id)
    provider = DeterministicCourseProvider()
    containers = FakeMultiPluginContainerRuntime()
    runtime = PluginHostRuntime(
        settings,
        database,
        container_runtime=containers,
        media_projector=ManualMediaProjector(database),  # type: ignore[arg-type]
        peer_factory=lambda process: process.peer,
        structured_provider=provider,
    )
    course_package = _course_package(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )
    diagnostic_package = _diagnostic_package(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )

    with TestClient(
        create_app(settings=settings, database=database, plugin_host_runtime=runtime)
    ) as client:
        _install_and_enable(client, course_package)
        _install_and_enable(
            client,
            diagnostic_package,
            plugin_id=DIAGNOSTIC_PLUGIN_ID,
        )
        bridge = client.get(f"/api/sessions/{session_id}/media-session")
        assert bridge.status_code == 200, bridge.text
        media_session_id = bridge.json()["media_session_id"]

        diagnostic_identity = PluginIdentity(
            DIAGNOSTIC_PLUGIN_ID,
            PLUGIN_VERSION,
        )
        diagnostic_binding = runtime.supervisor.binding(
            diagnostic_identity,
            media_session_id,
        )
        diagnostic_peer = containers.processes[
            (DIAGNOSTIC_PLUGIN_ID, PLUGIN_VERSION)
        ][-1].peer
        assert isinstance(diagnostic_peer, DiagnosticPeer)
        client.portal.call(
            diagnostic_peer.invoke_host,
            {
                "capability": "ui.publish",
                "session_scope": diagnostic_binding.scope,
                "input": {
                    "surface": "panel",
                    "view_id": "main",
                    "view_version": 1,
                    "view": {
                        "id": "diagnostic-refresh",
                        "type": "button",
                        "label": "Refresh diagnostic",
                        "action_id": "refresh-action",
                    },
                    "actions": [
                        {
                            "id": "refresh-action",
                            "kind": "command",
                            "command": "refresh",
                        }
                    ],
                },
            },
        )

        views = _wait_for(
            lambda: (
                listed
                if len(
                    listed := client.get(
                        f"/api/media-sessions/{media_session_id}/plugin-views"
                    ).json()
                )
                == 2
                else None
            ),
            message="two plugin views",
        )
        by_plugin = {item["plugin_id"]: item for item in views}
        assert set(by_plugin) == {PLUGIN_ID, DIAGNOSTIC_PLUGIN_ID}
        assert by_plugin[PLUGIN_ID]["session_scope"] != diagnostic_binding.scope

        diagnostic_view = by_plugin[DIAGNOSTIC_PLUGIN_ID]
        diagnostic_command = client.post(
            f"/api/media-sessions/{media_session_id}/plugin-commands",
            json={
                "plugin_id": DIAGNOSTIC_PLUGIN_ID,
                "plugin_version": diagnostic_view["plugin_version"],
                "session_scope": diagnostic_view["session_scope"],
                "surface": diagnostic_view["surface"],
                "view_id": diagnostic_view["view_id"],
                "expected_view_version": diagnostic_view["view_version"],
                "action_id": "refresh-action",
                "values": {},
            },
        )
        assert diagnostic_command.status_code == 200, diagnostic_command.text
        assert diagnostic_peer.commands[-1]["command"] == "refresh"
        assert diagnostic_peer.commands[-1]["session_scope"] == diagnostic_binding.scope

        cross_plugin_scope = client.post(
            f"/api/media-sessions/{media_session_id}/plugin-commands",
            json={
                "plugin_id": PLUGIN_ID,
                "plugin_version": PLUGIN_VERSION,
                "session_scope": diagnostic_binding.scope,
                "surface": diagnostic_view["surface"],
                "view_id": diagnostic_view["view_id"],
                "expected_view_version": diagnostic_view["view_version"],
                "action_id": "refresh-action",
                "values": {},
            },
        )
        assert cross_plugin_scope.status_code == 403

        course_view = by_plugin[PLUGIN_ID]
        course_command = client.post(
            f"/api/media-sessions/{media_session_id}/plugin-commands",
            json={
                "plugin_id": PLUGIN_ID,
                "plugin_version": course_view["plugin_version"],
                "session_scope": course_view["session_scope"],
                "surface": course_view["surface"],
                "view_id": course_view["view_id"],
                "expected_view_version": course_view["view_version"],
                "action_id": "generate-final",
                "values": {},
            },
        )
        assert course_command.status_code == 200, course_command.text
        course_documents = _wait_for(
            lambda: (
                items
                if (
                    items := client.get(
                        f"/api/media-sessions/{media_session_id}/plugin-documents",
                        params={"plugin_id": PLUGIN_ID},
                    ).json()
                )
                else None
            ),
            message="course document in multi-plugin session",
        )
        all_documents = client.get(
            f"/api/media-sessions/{media_session_id}/plugin-documents"
        ).json()
        diagnostic_documents = client.get(
            f"/api/media-sessions/{media_session_id}/plugin-documents",
            params={"plugin_id": DIAGNOSTIC_PLUGIN_ID},
        ).json()
        assert all(item["plugin_id"] == PLUGIN_ID for item in course_documents)
        assert all_documents == course_documents
        assert diagnostic_documents == []
        assert len(diagnostic_peer.commands) == 1

    database.dispose()
