from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.assistant.context import ContextBuilder  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.models import (  # noqa: E402
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
)
from app.plugins.bootstrap import PluginHostRuntime  # noqa: E402
from app.plugins.builtin_packages import BuiltinPluginPackageService  # noqa: E402
from app.plugins.builtins import BuiltinPluginRegistry  # noqa: E402
from app.plugins.container_runtime import PluginIdentity  # noqa: E402
from app.plugins.repository import PluginRepository  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.text_processing.provider import (  # noqa: E402
    StructuredCompletionRequest,
    StructuredCompletionResult,
)
from scripts.package_course_organizer_plugin import package_course_plugin  # noqa: E402


PLUGIN_ID = "com.matinier.course-organizer"
PLUGIN_VERSION = "1.0.3"
SESSION_ID = "course-organizer-docker-smoke"
HOST_SECRET_NAME = "MATINIER_COURSE_SMOKE_SECRET"
ADMIN_TOKEN = "course-organizer-smoke-admin"


class SmokeStructuredProvider:
    provider_name = "smoke-fixture"
    model = "course-smoke-v1"

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        payload = dict(request.input_payload)
        task = str(payload.get("task"))
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("smoke provider expected items")
        if task == "course.realtime_notes":
            output = {
                "notes": [
                    {
                        "id": f"note-{item['item_id']}",
                        "note_type": item["classification"],
                        "title": f"Smoke note {item['start_ms']}",
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
                        "title": f"Smoke knowledge {index + 1}",
                        "statement": item["text"],
                        "explanation": "Derived only from frozen Package evidence.",
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
            raise ValueError(f"unexpected smoke model task: {task}")
        return StructuredCompletionResult(
            content=json.dumps(output, ensure_ascii=False),
            finish_reason="stop",
            output_tokens=32,
        )


def docker(*arguments: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    command = ("docker", *arguments)
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            command,
            124,
            error.stdout or "",
            error.stderr or "Docker command timed out",
        )


def quiet_command_runner(arguments, *, check: bool):
    return subprocess.run(
        arguments,
        check=check,
        capture_output=True,
        timeout=180,
    )


def wait_for(predicate, *, timeout: float = 20, label: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise TimeoutError(f"course smoke timed out waiting for {label}")


def add_caption_pair(
    database: Database,
    *,
    index: int,
    start_ms: int,
    source: str,
    translation: str,
) -> None:
    now = dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=index)
    segment_id = f"smoke-segment-{index}"
    with database.session() as db_session:
        db_session.add_all(
            [
                SegmentRecord(
                    id=str(uuid.uuid4()),
                    session_id=SESSION_ID,
                    segment_id=segment_id,
                    track_id="smoke-course-track",
                    revision=1,
                    language="en-US",
                    raw_text=source,
                    display_text=source,
                    audio_start_ms=start_ms,
                    audio_end_ms=start_ms + 1_500,
                    confidence=0.99,
                    status="final",
                    received_at_ms=start_ms + 1_500,
                    finalized_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                TranslationSegmentRecord(
                    id=str(uuid.uuid4()),
                    session_id=SESSION_ID,
                    segment_id=f"smoke-translation-{index}",
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


def seed_course(database: Database) -> None:
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id=SESSION_ID,
                room_name=SESSION_ID,
                status="active",
                source_type="browser-tab",
                source_name="Course smoke tab",
                language="en-US",
                target_language="zh-CN",
                translation_status="running",
            )
        )
        db_session.commit()
    add_caption_pair(
        database,
        index=1,
        start_ms=0,
        source="Gradient is defined as the vector of partial derivatives.",
        translation="梯度定义为偏导数组成的向量。",
    )
    add_caption_pair(
        database,
        index=2,
        start_ms=61_000,
        source="For example, gradient descent moves opposite the gradient.",
        translation="例如，梯度下降沿梯度反方向移动。",
    )


def walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def view(client: TestClient, media_session_id: str) -> dict[str, object]:
    response = client.get(f"/api/media-sessions/{media_session_id}/plugin-views")
    if response.status_code != 200:
        raise RuntimeError("course plugin view API failed")
    values = response.json()
    if len(values) != 1:
        raise RuntimeError("course plugin did not publish exactly one view")
    return values[0]


def execute_action(
    client: TestClient,
    media_session_id: str,
    action_id: str,
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    current = view(client, media_session_id)
    response = client.post(
        f"/api/media-sessions/{media_session_id}/plugin-commands",
        json={
            "plugin_id": current["plugin_id"],
            "plugin_version": current["plugin_version"],
            "session_scope": current["session_scope"],
            "surface": current["surface"],
            "view_id": current["view_id"],
            "expected_view_version": current["view_version"],
            "action_id": action_id,
            "values": values or {},
        },
    )
    if response.status_code != 200:
        raise RuntimeError("course plugin command API failed")
    return response.json()


def list_documents(client: TestClient, media_session_id: str) -> list[dict[str, object]]:
    response = client.get(
        f"/api/media-sessions/{media_session_id}/plugin-documents"
    )
    if response.status_code != 200:
        raise RuntimeError("course plugin document list API failed")
    return response.json()


def find_document(
    client: TestClient,
    media_session_id: str,
    *,
    language: str,
    completeness: str,
):
    return next(
        (
            item
            for item in list_documents(client, media_session_id)
            if item["language"] == language
            and item["completeness"] == completeness
        ),
        None,
    )


def inspect_isolation(container_id: str) -> tuple[bool, bool]:
    inspected = docker("inspect", container_id)
    if inspected.returncode != 0:
        return False, False
    payload = json.loads(inspected.stdout)[0]
    host_config = payload["HostConfig"]
    security = host_config.get("SecurityOpt") or []
    cap_drop = {str(item).upper() for item in host_config.get("CapDrop") or []}
    isolated = (
        host_config.get("NetworkMode") == "none"
        and payload.get("Mounts") == []
        and host_config.get("ReadonlyRootfs") is True
        and "ALL" in cap_drop
        and any("no-new-privileges" in str(item) for item in security)
    )
    direct_network = docker(
        "exec",
        container_id,
        "python",
        "-c",
        "import socket; socket.create_connection(('1.1.1.1',53),1)",
        timeout=10,
    )
    secret_check = docker(
        "exec",
        container_id,
        "python",
        "-c",
        (
            "import os,sys;"
            f"sys.exit(0 if {HOST_SECRET_NAME!r} not in os.environ else 1)"
        ),
        timeout=10,
    )
    return isolated and direct_network.returncode != 0, secret_check.returncode == 0


def quiet_builtin_builder(**kwargs) -> None:
    package_course_plugin(**kwargs, command_runner=quiet_command_runner)


def run_smoke(
    root: Path,
) -> tuple[dict[str, object], set[str], set[str]]:
    os.environ[HOST_SECRET_NAME] = "host-only-course-smoke-secret"

    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="smoke-key",
        livekit_api_secret="smoke-secret-that-is-at-least-32-bytes",
        livekit_room_name=SESSION_ID,
        database_url=f"sqlite:///{(root / 'course-smoke.db').as_posix()}",
        data_dir=root / "data",
        plugin_admin_token=ADMIN_TOKEN,
        plugin_builtin_build_enabled=True,
        plugin_builtin_work_dir=root / "builtin-work",
        plugin_rpc_timeout_seconds=8,
        plugin_shutdown_timeout_seconds=5,
    )
    database = Database(settings.database_url)
    database.create_schema()
    seed_course(database)
    builtin_packages = BuiltinPluginPackageService(
        settings=settings,
        registry=BuiltinPluginRegistry(settings),
        builder=quiet_builtin_builder,
    )
    runtime = PluginHostRuntime(
        settings,
        database,
        structured_provider=SmokeStructuredProvider(),
        builtin_package_service=builtin_packages,
    )
    identity = PluginIdentity(PLUGIN_ID, PLUGIN_VERSION)
    created_containers: set[str] = set()
    created_images: set[str] = set()
    result: dict[str, object] = {
        "plugin_ready": False,
        "realtime_notes": False,
        "continuous_delivery": False,
        "manual_interim": False,
        "terminal_complete": False,
        "evidence_closed": False,
        "exports_match": False,
        "language_selected": False,
        "cursor_resumed": False,
        "network_isolated": False,
        "secret_blocked": False,
        "caption_regression": True,
    }
    try:
        with TestClient(
            create_app(
                settings=settings,
                database=database,
                plugin_host_runtime=runtime,
            )
        ) as client:
            headers = {
                "X-Plugin-Admin-Token": ADMIN_TOKEN,
            }
            inspected = client.post(
                f"/api/plugins/builtins/{PLUGIN_ID}/packages:inspect",
                headers=headers,
            )
            if inspected.status_code != 200:
                raise RuntimeError("built-in course plugin inspection failed")
            inspection = inspected.json()
            installed = client.post(
                "/api/plugins/installations",
                headers={"X-Plugin-Admin-Token": ADMIN_TOKEN},
                json={
                    "ticket_id": inspection["ticket_id"],
                    "accepted_permissions": inspection["permissions"],
                    "trust_publisher": True,
                    "approved_publisher_fingerprint": inspection[
                        "publisher_fingerprint"
                    ],
                },
            )
            if installed.status_code != 201:
                raise RuntimeError("signed course plugin installation failed")
            with database.session() as db_session:
                package = PluginRepository(db_session).get_package(
                    PLUGIN_ID,
                    PLUGIN_VERSION,
                )
                if package is None or not package.runtime_image_ref:
                    raise RuntimeError("course plugin image reference was not recorded")
                created_images.add(package.runtime_image_ref)
            enabled = client.post(
                f"/api/plugins/{PLUGIN_ID}/enable",
                headers={"X-Plugin-Admin-Token": ADMIN_TOKEN},
            )
            result["plugin_ready"] = (
                enabled.status_code == 200
                and runtime.supervisor.status(identity) == "ready"
            )

            bridge = client.get(f"/api/sessions/{SESSION_ID}/media-session")
            if bridge.status_code != 200:
                raise RuntimeError("course smoke media bridge failed")
            media_session_id = bridge.json()["media_session_id"]
            wait_for(
                lambda: any(
                    isinstance(item, dict) and "time_ms" in item
                    for item in walk(view(client, media_session_id)["view"])
                ),
                label="real-container realtime notes",
            )
            result["realtime_notes"] = True

            first_container = runtime.supervisor.container_id(identity)
            if first_container is None:
                raise RuntimeError("course smoke container was not started")
            created_containers.add(first_container)
            network_isolated, secret_blocked = inspect_isolation(first_container)
            result["network_isolated"] = network_isolated
            result["secret_blocked"] = secret_blocked

            execute_action(client, media_session_id, "generate-final")
            interim = wait_for(
                lambda: find_document(
                    client,
                    media_session_id,
                    language="zh-CN",
                    completeness="interim",
                ),
                label="real-container interim document",
            )
            result["manual_interim"] = interim["document_version"] == 1
            result["interim_document_id"] = interim["document_id"]
            result["interim_document_hash"] = interim["content_hash"]
            result["interim_package_hash"] = interim["source_package_hash"]

            initial_realtime_ids = {
                item["id"] for item in walk(view(client, media_session_id)["view"])
                if isinstance(item, dict) and "time_ms" in item
            }
            add_caption_pair(
                database,
                index=3,
                start_ms=90_000,
                source="Because learning rate controls step size, it affects stability.",
                translation="因为学习率控制步长，所以它会影响稳定性。",
            )
            add_caption_pair(
                database,
                index=4,
                start_ms=151_000,
                source="For example, a large learning rate can diverge.",
                translation="例如，过大的学习率会导致发散。",
            )
            wait_for(
                lambda: any(
                    isinstance(item, dict) and "time_ms" in item
                    and item["id"] not in initial_realtime_ids
                    for item in walk(view(client, media_session_id)["view"])
                ),
                label="continued realtime notes without reconnect",
            )
            result["continuous_delivery"] = True
            with database.session() as db_session:
                session = db_session.get(SessionRecord, SESSION_ID)
                if session is None:
                    raise RuntimeError("course smoke Session disappeared")
                session.status = "completed"
                session.stop_reason = "source_ended"
                session.ended_at = dt.datetime.now(dt.UTC)
                db_session.commit()
            # Only poll read-only document endpoints: terminal delivery must not
            # depend on explicitly resolving/reconnecting the MediaSession.
            complete = wait_for(
                lambda: find_document(
                    client,
                    media_session_id,
                    language="zh-CN",
                    completeness="complete",
                ),
                label="real-container complete document",
            )
            result["terminal_complete"] = complete["document_version"] == 2
            result["complete_document_id"] = complete["document_id"]
            result["complete_document_hash"] = complete["content_hash"]
            result["complete_package_id"] = complete["source_package_id"]
            result["complete_package_hash"] = complete["source_package_hash"]

            detail_response = client.get(
                f"/api/plugin-documents/{complete['document_id']}"
            )
            detail = detail_response.json()
            realtime_part = next(part for part in detail["content"]["parts"]
                                 if part["id"] == "realtime-notes")
            result["realtime_notes_in_final"] = bool(realtime_part["items"])
            if not result["realtime_notes_in_final"]:
                raise RuntimeError("realtime notes were lost when closing Package evidence")
            evidence_ids = {item["item_id"] for item in detail["evidence_refs"]}
            knowledge = next(
                item for item in detail["content"]["parts"] if item["id"] == "knowledge"
            )
            knowledge_items = [
                item
                for category in knowledge["categories"]
                for item in category["items"]
            ]
            frozen = client.get(
                f"/api/packages/{complete['source_package_id']}"
            ).json()
            package_ids = {
                item["item_id"]
                for document in frozen["documents"]
                if document["document_kind"] == "evidence_index"
                for item in document["content"]["items"]
            }
            result["evidence_closed"] = bool(knowledge_items) and all(
                set(item["evidence_item_ids"]) <= evidence_ids <= package_ids
                for item in knowledge_items
            )
            markdown = client.get(
                f"/api/plugin-documents/{complete['document_id']}/export?format=markdown"
            )
            json_export = client.get(
                f"/api/plugin-documents/{complete['document_id']}/export?format=json"
            )
            result["exports_match"] = (
                markdown.status_code == 200
                and markdown.text == detail["markdown"]
                and json_export.status_code == 200
                and json_export.json()["metadata"]["content_hash"]
                == complete["content_hash"]
            )

            execute_action(
                client,
                media_session_id,
                "set-language",
                {"output_language": "fr-FR"},
            )
            wait_for(
                lambda: "应用语言：fr-FR"
                in json.dumps(view(client, media_session_id)["view"], ensure_ascii=False),
                label="real-container language switch",
            )
            execute_action(client, media_session_id, "generate-final")
            french = wait_for(
                lambda: find_document(
                    client,
                    media_session_id,
                    language="fr-FR",
                    completeness="interim",
                ),
                label="real-container language document",
            )
            result["language_selected"] = (
                french["identity_key"] == "course-notes:fr-FR"
                and french["document_version"] == 1
            )

            binding = runtime.supervisor.binding(identity, media_session_id)
            initial_generation = binding.generation
            acknowledged = binding.last_acknowledged_sequence
            before_restart = len(list_documents(client, media_session_id))
            killed = docker("kill", first_container)
            if killed.returncode != 0:
                raise RuntimeError("course smoke failed to kill its container")

            def restored_binding():
                try:
                    current = runtime.supervisor.binding(identity, media_session_id)
                except LookupError:
                    return None
                return (
                    current
                    if runtime.supervisor.status(identity) == "ready"
                    and current.generation > initial_generation
                    else None
                )

            restored = wait_for(restored_binding, label="real-container cursor restore")
            second_container = runtime.supervisor.container_id(identity)
            if second_container is not None:
                created_containers.add(second_container)
            time.sleep(0.2)
            result["cursor_resumed"] = (
                restored.last_acknowledged_sequence == acknowledged
                and len(list_documents(client, media_session_id)) == before_restart
            )

            with database.session() as db_session:
                snapshot = client.portal.call(
                    lambda: ContextBuilder(db_session).build(
                        session_id=SESSION_ID,
                        goal="Read the course smoke caption",
                        persist=False,
                    )
                )
            result["caption_regression"] = not any(
                item.message_kind == "caption"
                and "gradient is defined" in item.display_text.casefold()
                for item in snapshot.evidence_messages
            )
    finally:
        try:
            if runtime.supervisor.is_supervised(identity):
                current = runtime.supervisor.container_id(identity)
                if current is not None:
                    created_containers.add(current)
        except Exception:
            pass
        database.dispose()
        os.environ.pop(HOST_SECRET_NAME, None)
    return result, created_containers, created_images


def cleanup(created_containers: set[str], created_images: set[str]) -> None:
    for container_id in sorted(created_containers):
        docker("container", "rm", "--force", container_id)
    for image_ref in sorted(created_images):
        docker("image", "rm", "--force", image_ref)


def main() -> None:
    # The smoke result is a machine-readable acceptance artifact.  Application
    # request logs are intentionally suppressed so stdout contains one JSON line.
    result: dict[str, object]
    created_containers: set[str] = set()
    created_images: set[str] = set()
    try:
        with tempfile.TemporaryDirectory(
            prefix="matinier-course-organizer-smoke-"
        ) as raw:
            result, created_containers, created_images = run_smoke(Path(raw))
    finally:
        cleanup(created_containers, created_images)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    required_true = (
        "plugin_ready",
        "realtime_notes",
        "continuous_delivery",
        "realtime_notes_in_final",
        "manual_interim",
        "terminal_complete",
        "evidence_closed",
        "exports_match",
        "language_selected",
        "cursor_resumed",
        "network_isolated",
        "secret_blocked",
    )
    if not all(result.get(key) is True for key in required_true):
        raise SystemExit(1)
    if result.get("caption_regression") is not False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
