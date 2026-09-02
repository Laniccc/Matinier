from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant.context import ContextBuilder
from app.persistence.database import Database
from app.persistence.models import SegmentRecord, SessionRecord
from app.plugins.bootstrap import PluginHostRuntime
from app.plugins.container_runtime import PluginIdentity
from app.plugins.repository import PluginRepository
from app.settings import Settings
from scripts.package_diagnostic_plugin import package_plugin


PLUGIN_ID = "com.matinier.diagnostic"
PLUGIN_VERSION = "1.0.0"
IMAGE_TAG = "matinier-diagnostic-plugin:1.0.0"


def seed_caption(database: Database) -> None:
    now = dt.datetime.now(dt.UTC)
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id="plugin-framework-smoke",
                room_name="plugin-framework-smoke",
                status="completed",
                stop_reason="source_ended",
                source_type="browser-tab",
                source_name="Smoke source",
                language="en-US",
                ended_at=now,
            )
        )
        db_session.flush()
        db_session.add(
            SegmentRecord(
                id=str(uuid.uuid4()),
                session_id="plugin-framework-smoke",
                segment_id="smoke-final-1",
                track_id="smoke-track",
                revision=1,
                language="en-US",
                raw_text="Plugin framework smoke caption",
                display_text="Plugin framework smoke caption.",
                audio_start_ms=0,
                audio_end_ms=750,
                confidence=0.99,
                status="final",
                received_at_ms=1_000,
                finalized_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db_session.commit()


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


async def wait_for(
    predicate,
    *,
    timeout: float = 15,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("plugin framework smoke condition timed out")
        await asyncio.sleep(0.05)


async def run_smoke(root: Path) -> dict[str, object]:
    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="smoke-key",
        livekit_api_secret="smoke-secret-that-is-at-least-32-bytes",
        livekit_room_name="plugin-framework-smoke",
        database_url=f"sqlite:///{(root / 'smoke.db').as_posix()}",
        data_dir=root / "data",
        plugin_admin_token="plugin-framework-smoke-admin",
        plugin_rpc_timeout_seconds=5,
        plugin_shutdown_timeout_seconds=5,
    )
    database = Database(settings.database_url)
    database.create_schema()
    seed_caption(database)
    runtime = PluginHostRuntime(settings, database)
    identity = PluginIdentity(PLUGIN_ID, PLUGIN_VERSION)
    plugin_ready = False
    cursor_resumed = False
    network_isolated = False
    caption_regression = True
    try:
        if not await runtime.container_runtime.available():
            raise RuntimeError("Docker daemon is unavailable")
        private_key = Ed25519PrivateKey.generate()
        key_path = root / "diagnostic-key.pem"
        key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        package_path = root / "diagnostic.plugin.zip"
        package_plugin(
            output=package_path,
            private_key_path=key_path,
            image_tag=IMAGE_TAG,
        )

        await runtime.start()
        inspection = runtime.inspect_package(package_path)
        await runtime.confirm_install(
            ticket_id=inspection.ticket_id,
            accepted_permissions=inspection.permissions,
            trust_publisher=True,
            approved_publisher_fingerprint=inspection.publisher_fingerprint,
        )
        await runtime.enable_plugin(PLUGIN_ID)
        plugin_ready = runtime.supervisor.status(identity) == "ready"

        bridge = await runtime.resolve_media_session("plugin-framework-smoke")
        media_session_id = str(bridge["media_session_id"])
        await wait_for(lambda: bool(runtime.list_plugin_views(media_session_id)))
        binding = runtime.supervisor.binding(identity, media_session_id)
        acknowledged = binding.last_acknowledged_sequence
        if acknowledged < 1:
            raise RuntimeError("diagnostic plugin did not acknowledge the Final event")

        container_id = runtime.supervisor.container_id(identity)
        if container_id is None:
            raise RuntimeError("diagnostic plugin container is unavailable")
        network_mode = docker(
            "inspect",
            "--format",
            "{{.HostConfig.NetworkMode}}",
            container_id,
        )
        direct_network = docker(
            "exec",
            container_id,
            "python",
            "-c",
            "import socket; socket.create_connection(('1.1.1.1', 53), 1)",
            timeout=10,
        )
        network_isolated = (
            network_mode.returncode == 0
            and network_mode.stdout.strip() == "none"
            and direct_network.returncode != 0
        )

        initial_generation = binding.generation
        killed = docker("kill", container_id)
        if killed.returncode != 0:
            raise RuntimeError("failed to terminate diagnostic plugin container")
        await wait_for(
            lambda: (
                runtime.supervisor.status(identity) == "ready"
                and runtime.supervisor.binding(identity, media_session_id).generation
                > initial_generation
            )
        )
        restored = runtime.supervisor.binding(identity, media_session_id)
        cursor_resumed = restored.last_acknowledged_sequence == acknowledged

        with database.session() as db_session:
            snapshot = await ContextBuilder(db_session).build(
                session_id="plugin-framework-smoke",
                goal="Read the smoke caption",
                persist=False,
            )
            caption_regression = not any(
                item.message_kind == "caption"
                and "plugin framework smoke caption" in item.display_text.casefold()
                for item in snapshot.evidence_messages
            )

        await runtime.disable_plugin(PLUGIN_ID)
        with database.session() as db_session:
            installation = PluginRepository(db_session).get_installation(PLUGIN_ID)
            if installation is None or installation.status != "disabled":
                raise RuntimeError("diagnostic plugin did not disable cleanly")
    finally:
        await runtime.stop()
        database.dispose()

    return {
        "plugin_ready": plugin_ready,
        "cursor_resumed": cursor_resumed,
        "network_isolated": network_isolated,
        "caption_regression": caption_regression,
    }


def main() -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="matinier-plugin-framework-smoke-") as raw:
            result = asyncio.run(run_smoke(Path(raw)))
    finally:
        docker("image", "rm", "--force", IMAGE_TAG)
    print(json.dumps(result, separators=(",", ":")))
    if not (
        result["plugin_ready"] is True
        and result["cursor_resumed"] is True
        and result["network_isolated"] is True
        and result["caption_regression"] is False
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
