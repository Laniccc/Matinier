from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

from app.plugins.contracts import PluginResourceLimits


_SAFE_ID = re.compile(r"^[a-z][a-z0-9.-]{2,127}$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}
_HOST_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "WINDIR")


@dataclass(frozen=True, slots=True)
class PluginIdentity:
    plugin_id: str
    version: str


@dataclass(frozen=True, slots=True)
class PluginContainerSpec:
    plugin_id: str
    version: str
    image_ref: str
    command: tuple[str, ...] = ()
    resources: PluginResourceLimits = field(default_factory=PluginResourceLimits)
    host_api_requirement: str = ">=1.0 <2.0"

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.plugin_id):
            raise ValueError("invalid plugin id")
        if not _SAFE_VERSION.fullmatch(self.version):
            raise ValueError("invalid plugin version")
        if not self.image_ref or any(character.isspace() for character in self.image_ref):
            raise ValueError("invalid image reference")
        executable = Path(self.command[0]).name.casefold() if self.command else ""
        has_combined_command = bool(self.command) and len(self.command) == 1 and any(
            character.isspace() for character in self.command[0]
        )
        if executable in _SHELL_EXECUTABLES or has_combined_command:
            raise ValueError("plugin command must use a direct executable argument array")
        if any(not item or "\x00" in item or "\r" in item or "\n" in item for item in self.command):
            raise ValueError("plugin command contains an invalid executable argument")

    @property
    def identity(self) -> PluginIdentity:
        return PluginIdentity(self.plugin_id, self.version)


@dataclass(frozen=True, slots=True)
class ImportedImage:
    archive_digest: str
    image_ref: str


@dataclass(slots=True)
class PluginContainerProcess:
    container_id: str
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    _process: asyncio.subprocess.Process

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        return await self._process.wait()


class ContainerRuntime(Protocol):
    async def available(self) -> bool: ...

    async def import_image(
        self,
        image_tar: Path,
        expected_digest: str,
    ) -> ImportedImage: ...

    async def start(self, spec: PluginContainerSpec) -> PluginContainerProcess: ...

    async def stop(self, container_id: str, *, timeout: float) -> None: ...


class DockerContainerRuntime:
    """Starts plugin OCI images with a closed, immutable Docker sandbox profile."""

    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        host_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._docker_binary = docker_binary
        source = os.environ if host_environment is None else host_environment
        self._subprocess_environment = {
            key: source[key]
            for key in _HOST_ENV_ALLOWLIST
            if key in source and source[key]
        }
        if "PATH" not in self._subprocess_environment:
            self._subprocess_environment["PATH"] = os.defpath

    @property
    def subprocess_environment(self) -> dict[str, str]:
        return dict(self._subprocess_environment)

    def build_run_arguments(
        self,
        spec: PluginContainerSpec,
        *,
        container_name: str,
    ) -> tuple[str, ...]:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", container_name):
            raise ValueError("invalid container name")
        cpu = format(spec.resources.cpu_count, "g")
        return (
            self._docker_binary,
            "run",
            "--rm",
            "-i",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(spec.resources.pids),
            "--memory",
            f"{spec.resources.memory_mb}m",
            "--cpus",
            cpu,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={spec.resources.tmpfs_mb}m",
            spec.image_ref,
            *spec.command,
        )

    async def available(self) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker_binary,
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._subprocess_environment,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            return process.returncode == 0 and bool(stdout.strip())
        except (FileNotFoundError, OSError, TimeoutError):
            return False

    async def import_image(
        self,
        image_tar: Path,
        expected_digest: str,
    ) -> ImportedImage:
        actual = "sha256:" + await asyncio.to_thread(_sha256_file, image_tar)
        if not _constant_time_equal(actual, expected_digest.casefold()):
            raise ValueError("plugin image archive digest does not match manifest")
        process = await asyncio.create_subprocess_exec(
            self._docker_binary,
            "image",
            "load",
            "--input",
            str(image_tar.resolve()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_environment,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("Docker rejected the verified plugin image archive")
        image_ref = _loaded_image_reference(stdout)
        return ImportedImage(archive_digest=actual, image_ref=image_ref)

    async def start(self, spec: PluginContainerSpec) -> PluginContainerProcess:
        suffix = uuid.uuid4().hex[:12]
        stem = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"plugin-{spec.plugin_id}-{spec.version}")
        container_name = f"{stem[:100]}-{suffix}"
        arguments = self.build_run_arguments(spec, container_name=container_name)
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_environment,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise RuntimeError("Docker plugin transport pipes were not created")
        return PluginContainerProcess(
            container_id=container_name,
            stdin=process.stdin,
            stdout=process.stdout,
            stderr=process.stderr,
            _process=process,
        )

    async def stop(self, container_id: str, *, timeout: float) -> None:
        if timeout <= 0:
            raise ValueError("container stop timeout must be positive")
        grace = max(1, int(timeout))
        process = await asyncio.create_subprocess_exec(
            self._docker_binary,
            "stop",
            "--time",
            str(grace),
            container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._subprocess_environment,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout + 2)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(
        left.encode("ascii", errors="ignore"),
        right.encode("ascii", errors="ignore"),
    )


def _loaded_image_reference(stdout: bytes) -> str:
    for raw_line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        line = raw_line.strip()
        for prefix in ("Loaded image: ", "Loaded image ID: "):
            if line.startswith(prefix) and line[len(prefix) :].strip():
                return line[len(prefix) :].strip()
    raise RuntimeError("Docker image load did not return an image reference")
