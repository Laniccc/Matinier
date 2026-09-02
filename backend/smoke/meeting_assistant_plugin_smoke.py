"""Real network-isolated OCI plugin, temporary Host, fake model and fake Linear.

Run from the source checkout with backend development dependencies installed.
Never reads .env or installs into the running application. Cleanup is ownership-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import tempfile
import uuid
import warnings
from pathlib import Path

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated.*")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from app.plugins.container_runtime import DockerContainerRuntime, PluginIdentity
from app.plugins.ui_schema import parse_plugin_view
from scripts.package_meeting_assistant_plugin import package_meeting_plugin
from meeting_plugin_e2e_support import Harness, PLUGIN, VERSION, ADMIN, wait_for

REQUIRED = ("signed_package", "network_isolated", "activation_required", "backlog_processed",
    "view_valid", "disable_blocks_new_writes", "history_preserved", "recovery_no_duplicate")


def docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


class OwnedDocker(DockerContainerRuntime):
    def __init__(self):
        super().__init__()
        self.owned_containers = set()

    async def start(self, spec):
        if spec.plugin_id != PLUGIN:
            raise AssertionError("Smoke may only start its meeting plugin")
        process = await super().start(spec)
        self.owned_containers.add(process.container_id)
        return process

    def cleanup(self):
        for identifier in self.owned_containers:
            if not identifier.startswith("plugin-com.matinier.meeting-assistant-"):
                raise AssertionError("Invalid owned container identity")
            docker("container", "rm", "--force", identifier)


def build_command(command, *, check):
    result = subprocess.run(command, capture_output=True, text=True, timeout=240, check=False)
    if check and result.returncode:
        # Docker output may include environment configuration; do not publish it.
        raise RuntimeError("isolated_package_build_failed")
    return result


def run_smoke(root, owned, image_tag, result):
    key = root / "ephemeral-key.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    package = root / "meeting.plugin.zip"
    package_meeting_plugin(output=package, private_key_path=key, image_tag=image_tag, command_runner=build_command)
    h = Harness(root, containers=owned, real_docker=True)
    identity = PluginIdentity(PLUGIN, VERSION)
    with h:
        h.install(package)
        result["signed_package"] = True
        h.bind()
        wait_for(h.view, "real container view")
        container = h.runtime.supervisor.container_id(identity)
        inspection = docker("inspect", "--format", "{{json .HostConfig}}", container)
        config = json.loads(inspection.stdout)
        mounts = docker("inspect", "--format", "{{json .Mounts}}", container)
        result["network_isolated"] = (config["NetworkMode"] == "none" and config["ReadonlyRootfs"]
            and not config.get("Binds") and json.loads(mounts.stdout) == [])
        assert result["network_isolated"]
        result["activation_required"] = h.extractor.calls == [] and h.provider.calls == []
        assert h.action("meeting.analysis.activate")["status"] == "applied"
        wait_for(lambda: h.history()["view"]["processing"]["pending_finals"] == 0, "backlog")
        result["backlog_processed"] = bool(h.extractor.calls)
        parse_plugin_view(h.view()["view"], allowed_commands=frozenset(json.loads(
            (BACKEND.parent / "plugin-sdk/examples/meeting-assistant/plugin.json").read_text("utf-8"))["commands"]))
        result["view_valid"] = True
        answer = h.settled(h.action("meeting.ask", {"message": "Explain release notes"}))
        assert answer["execution_status"] == "completed"
        wait_for(lambda: "Acceptance answer" in json.dumps(h.view()), "real answer display")
        preview = h.prepare("meeting.execute", {"message": "Create release notes",
            "candidate_ids": ["meeting-candidate"]})
        assert h.linear.create_calls == 0 and preview["confirmation_required"]
        admitted = h.confirm(preview)
        assert h.settled(admitted)["execution_status"] == "completed"
        assert h.confirm(preview) == admitted
        result["external_create_count"] = h.linear.create_calls

        # Kill only the recorded smoke container; no existing application container.
        generation = h.runtime.supervisor.binding(identity, "media-a").generation
        assert docker("kill", container).returncode == 0
        def recovered():
            try:
                return h.runtime.supervisor.status(identity) == "ready" and h.runtime.supervisor.binding(identity, "media-a").generation > generation
            except LookupError:
                return False
        wait_for(recovered, "container crash recovery")
        assert h.confirm(preview) == admitted and h.linear.create_calls == 1

        h.client.portal.call(h.assistant.operation_worker.stop)
        queued = h.action("meeting.ask", {"message": "Do not execute after disable"})
        calls = list(h.provider.calls)
        assert h.client.post(f"/api/plugins/{PLUGIN}/disable", headers=ADMIN).status_code == 200
        h.client.portal.call(h.assistant.operation_worker.start)
        assert h.settled(queued)["status"] == "cancelled"
        result["disable_blocks_new_writes"] = h.provider.calls == calls and h.linear.create_calls == 1
        history_ids = {e["execution_id"] for e in h.history()["view"]["executions"]}
        result["history_preserved"] = {answer["execution_id"], h.operation(admitted["operation_id"])["execution_id"], "meeting-completed"} <= history_ids
    restarted = Harness(root, containers=owned, real_docker=True, seed=False)
    with restarted:
        result["recovery_no_duplicate"] = (restarted.provider.calls == [] and restarted.linear.create_calls == 0
            and restarted.operation(admitted["operation_id"])["execution_status"] == "completed")
        assert {e["execution_id"] for e in restarted.history()["view"]["executions"]} == history_ids


def main():
    logging.disable(logging.CRITICAL)
    result = {key: False for key in REQUIRED}
    result["external_create_count"] = 0
    owned = OwnedDocker()
    tag = "matinier-meeting-smoke:" + uuid.uuid4().hex
    try:
        if not asyncio.run(owned.available()):
            raise RuntimeError("docker_unavailable")
        with tempfile.TemporaryDirectory(prefix="matinier-meeting-smoke-") as raw:
            root = Path(raw).resolve(strict=True)
            if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("matinier-meeting-smoke-"):
                raise AssertionError("Unexpected temporary root")
            run_smoke(root, owned, tag, result)
    except Exception as error:
        result["failure_type"] = type(error).__name__
    finally:
        owned.cleanup()
        # The UUID tag was created by this invocation; never prune shared images.
        docker("image", "rm", "--force", tag)
    print(json.dumps(result, separators=(",", ":")))
    return 0 if all(result[k] is True for k in REQUIRED) and result["external_create_count"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
