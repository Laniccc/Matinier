from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.plugins.container_runtime import DockerContainerRuntime, PluginContainerSpec
from app.plugins.contracts import PluginResourceLimits


BASE_IMAGE = os.environ.get("PLUGIN_SANDBOX_SMOKE_BASE", "redis/redis-stack:latest")
IMAGE_TAG = "matinier-plugin-sandbox-smoke:local"


SMOKE_SCRIPT = r"""#!/bin/sh
sentinel_path="$1"

sentinel_blocked=true
if test -r "$sentinel_path"; then
  sentinel_blocked=false
fi

secret_blocked=true
if test -n "${PLUGIN_SANDBOX_SECRET+x}"; then
  secret_blocked=false
fi

network_blocked=true
if getent hosts example.com >/dev/null 2>&1; then
  network_blocked=false
fi

root_write_blocked=true
if touch /sandbox-escape >/dev/null 2>&1; then
  root_write_blocked=false
fi

private_state_ok=false
if mkdir -p /tmp/private-state && printf 'private' > /tmp/private-state/value; then
  private_state_ok=true
fi

rpc_ok=false
IFS= read -r rpc_line
if test "$rpc_line" = '{"jsonrpc":"2.0","id":"smoke","method":"plugin.heartbeat","params":{}}'; then
  rpc_ok=true
fi

printf '{"jsonrpc":"2.0","id":"smoke","result":{"sentinel_blocked":%s,"secret_blocked":%s,"network_blocked":%s,"root_write_blocked":%s,"private_state_ok":%s,"rpc_ok":%s}}\n' \
  "$sentinel_blocked" "$secret_blocked" "$network_blocked" "$root_write_blocked" "$private_state_ok" "$rpc_ok"
"""


def docker_environment() -> dict[str, str]:
    return DockerContainerRuntime().subprocess_environment


def build_smoke_image(context: Path) -> None:
    (context / "smoke.sh").write_text(SMOKE_SCRIPT, encoding="utf-8", newline="\n")
    (context / "Dockerfile").write_text(
        "\n".join(
            (
                f"FROM {BASE_IMAGE}",
                "COPY smoke.sh /sandbox-smoke.sh",
                'ENTRYPOINT ["/bin/sh", "/sandbox-smoke.sh"]',
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    completed = subprocess.run(
        (
            "docker",
            "build",
            "--pull=false",
            "--network",
            "none",
            "--tag",
            IMAGE_TAG,
            str(context),
        ),
        env=docker_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("sandbox smoke image build failed: " + completed.stderr[-1000:])


async def run_smoke(sentinel: Path) -> dict[str, object]:
    os.environ["PLUGIN_SANDBOX_SECRET"] = "host-only-do-not-leak"
    runtime = DockerContainerRuntime()
    if not await runtime.available():
        raise RuntimeError("Docker daemon is unavailable")
    process = await runtime.start(
        PluginContainerSpec(
            plugin_id="com.matinier.sandbox-smoke",
            version="1.0.0",
            image_ref=IMAGE_TAG,
            command=(sentinel.resolve().as_posix(),),
            resources=PluginResourceLimits(
                memory_mb=128,
                cpu_count=0.25,
                pids=32,
                tmpfs_mb=16,
            ),
        )
    )
    request = (
        b'{"jsonrpc":"2.0","id":"smoke","method":"plugin.heartbeat","params":{}}\n'
    )
    process.stdin.write(request)
    await process.stdin.drain()
    response_line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
    stderr = await asyncio.wait_for(process.stderr.read(), timeout=15)
    exit_code = await asyncio.wait_for(process.wait(), timeout=15)
    if exit_code != 0:
        raise RuntimeError(
            f"sandbox smoke container exited {exit_code}: "
            + stderr.decode("utf-8", errors="replace")[-1000:]
        )
    response = json.loads(response_line)
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("sandbox smoke returned an invalid JSON-RPC result")
    return result


def remove_smoke_image() -> None:
    subprocess.run(
        ("docker", "image", "rm", "--force", IMAGE_TAG),
        env=docker_environment(),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="matinier-plugin-smoke-") as raw_context:
        context = Path(raw_context)
        sentinel = context / "host-sentinel.txt"
        sentinel.write_text("host-only-sentinel", encoding="utf-8")
        try:
            build_smoke_image(context)
            result = asyncio.run(run_smoke(sentinel))
        finally:
            remove_smoke_image()
    required = (
        "sentinel_blocked",
        "secret_blocked",
        "network_blocked",
        "root_write_blocked",
        "private_state_ok",
        "rpc_ok",
    )
    sandbox_ok = all(result.get(field) is True for field in required)
    print(json.dumps({"sandbox_ok": sandbox_ok, **result}, separators=(",", ":")))
    if not sandbox_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
