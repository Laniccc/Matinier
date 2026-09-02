from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.plugins.container_runtime import (
    DockerContainerRuntime,
    PluginContainerSpec,
)
from app.plugins.contracts import PluginResourceLimits


def test_docker_run_arguments_are_fixed_and_isolated() -> None:
    runtime = DockerContainerRuntime(
        host_environment={
            "PATH": "C:\\Windows\\System32",
            "SYSTEMROOT": "C:\\Windows",
            "PLUGIN_SECRET": "must-not-leak",
            "DATABASE_URL": "sqlite:///private.db",
            "HOME": "C:\\Users\\person",
        }
    )
    spec = PluginContainerSpec(
        plugin_id="com.example.diagnostic",
        version="1.2.3",
        image_ref="sha256:" + "a" * 64,
        command=("python", "-m", "diagnostic_plugin"),
        resources=PluginResourceLimits(
            memory_mb=192,
            cpu_count=0.75,
            pids=32,
            tmpfs_mb=48,
        ),
    )

    arguments = runtime.build_run_arguments(spec, container_name="plugin-fixed")

    assert arguments == (
        "docker",
        "run",
        "--rm",
        "-i",
        "--name",
        "plugin-fixed",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "32",
        "--memory",
        "192m",
        "--cpus",
        "0.75",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=48m",
        "sha256:" + "a" * 64,
        "python",
        "-m",
        "diagnostic_plugin",
    )
    joined = " ".join(arguments).casefold()
    assert "docker.sock" not in joined
    assert "--network host" not in joined
    assert "database" not in joined
    assert ".env" not in joined
    assert "users\\person" not in joined
    assert runtime.subprocess_environment == {
        "PATH": "C:\\Windows\\System32",
        "SYSTEMROOT": "C:\\Windows",
    }


@pytest.mark.parametrize(
    "command",
    [
        ("sh", "-c", "echo unsafe"),
        ("bash", "-lc", "echo unsafe"),
        ("cmd.exe", "/c", "echo unsafe"),
        ("powershell", "-Command", "Write-Output unsafe"),
        ("python -m plugin",),
    ],
)
def test_shell_commands_and_combined_command_strings_are_rejected(
    command: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="executable argument"):
        PluginContainerSpec(
            plugin_id="com.example.diagnostic",
            version="1.0.0",
            image_ref="example:1",
            command=command,
        )


def test_image_archive_digest_is_verified_before_docker_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_tar = tmp_path / "plugin-image.tar"
    image_tar.write_bytes(b"not-the-approved-image")
    calls: list[tuple[object, ...]] = []

    async def fake_subprocess(*args: object, **_kwargs: object) -> object:
        calls.append(args)
        raise AssertionError("docker must not run for a digest mismatch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = DockerContainerRuntime()

    async def scenario() -> None:
        with pytest.raises(ValueError, match="digest"):
            await runtime.import_image(image_tar, "sha256:" + "0" * 64)

    asyncio.run(scenario())
    assert calls == []
