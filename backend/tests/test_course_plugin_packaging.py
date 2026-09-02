from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import zipfile
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.plugins.contracts import PluginManifest
from app.plugins.signing import assets_digest, verify_signature, SignatureEnvelope
from scripts.package_plugin import build_plugin_package
from scripts.package_course_organizer_plugin import (
    capture_course_plugin_sources,
    course_plugin_source_digest,
    package_course_plugin,
    write_course_plugin_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]
COURSE = ROOT / "plugin-sdk" / "examples" / "course-organizer"
SDK_PYTHON = ROOT / "plugin-sdk" / "python"


def private_key(path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, *, check: bool) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in args]
        self.calls.append(command)
        if command[:3] == ["docker", "image", "save"]:
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"deterministic fake OCI image")
        return subprocess.CompletedProcess(command, 0)


def test_course_manifest_dockerfile_and_ui_commands_are_closed() -> None:
    manifest = PluginManifest.model_validate_json(
        (COURSE / "plugin.json").read_text("utf-8")
    )
    assert manifest.id == "com.matinier.course-organizer"
    assert manifest.name == "课程内容整理"
    assert manifest.version == "1.0.3"
    assert manifest.publisher == "Matinier Development"
    assert manifest.host_api == ">=1.0 <2.0"
    assert manifest.subscriptions == (
        "session.cancelled",
        "session.completed",
        "session.failed",
        "transcript.final",
        "translation.final",
    )
    assert manifest.permissions == (
        "delivery.prepare",
        "delivery.query",
        "document.publish",
        "model.invoke",
        "state.get",
        "state.put",
        "ui.publish",
    )
    assert "network.fetch" not in manifest.permissions
    assert manifest.commands == (
        "generate_final",
        "retry_final",
        "select_version",
        "set_language",
    )
    assert 64 <= manifest.resources.memory_mb <= 4_096
    assert 0 < manifest.resources.cpu_count <= 4
    assert 16 <= manifest.resources.pids <= 512
    assert 16 <= manifest.resources.tmpfs_mb <= 1_024


def test_course_entrypoint_preserves_same_schema_state_during_update() -> None:
    for path in (COURSE, SDK_PYTHON):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location(
        "course_organizer_entrypoint_test",
        COURSE / "plugin.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.CourseOrganizerPlugin(object())
    snapshot = [
        {
            "namespace": "plugin:com.matinier.course-organizer:1.0.1:session",
            "key": "course-session",
            "value": {"schema_version": 1},
            "version": 3,
        }
    ]

    pending = [{**snapshot[0], "value": {"schema_version": 1, "terminal_job_id": "old-job",
                                        "terminal_trigger": "session_completed", "notes": []}}]
    upgraded = asyncio.run(plugin.migrate_state({
        "from_schema_version": 1, "to_schema_version": 1, "items": pending,
    }))
    assert upgraded["items"][0]["value"]["terminal_job_id"] != "old-job"
    assert upgraded["items"][0]["value"]["terminal_trigger"] == "session_completed"
    assert pending[0]["value"]["terminal_job_id"] == "old-job"
    legacy = [{**snapshot[0], "value": {"schema_version": 1, "last_sequence": 12,
               "notes": [{"source_segment_ids": ["translation:zh-CN:old"]}],
               "history": [{"document_id": "preserved"}]}}]
    replay = asyncio.run(plugin.migrate_state({
        "from_schema_version": 1, "to_schema_version": 1, "items": legacy,
    }))["items"][0]["value"]
    assert replay["last_sequence"] == 0 and replay["notes"] == []
    assert replay["history"] == [{"document_id": "preserved"}]
    assert legacy[0]["value"]["last_sequence"] == 12

    result = asyncio.run(
        plugin.migrate_state(
            {
                "from_schema_version": 1,
                "to_schema_version": 1,
                "items": snapshot,
            }
        )
    )

    assert result == {"items": snapshot}

    dockerfile = (COURSE / "Dockerfile").read_text("utf-8")
    assert "plugin-sdk/python/matinier_plugin" in dockerfile
    assert "plugin-sdk/examples/course-organizer/plugin.py" in dockerfile
    assert "plugin-sdk/examples/course-organizer/course_organizer" in dockerfile
    assert "COPY ." not in dockerfile and "ADD ." not in dockerfile
    assert all(token not in dockerfile.casefold() for token in ("pip install", "apk add", "apt-get", "curl ", "wget "))
    assert "USER plugin" in dockerfile


def test_generic_packager_uses_explicit_context_signs_and_is_deterministic(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context"
    source = context / "plugin"
    source.mkdir(parents=True)
    dockerfile = source / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", "utf-8")
    manifest_path = source / "plugin.json"
    manifest_path.write_text((COURSE / "plugin.json").read_text("utf-8"), "utf-8")
    key = private_key(tmp_path / "external-key.pem")
    first = tmp_path / "first.plugin.zip"
    second = tmp_path / "second.plugin.zip"
    runner = FakeDocker()

    build_plugin_package(
        source_dir=source,
        build_context=context,
        dockerfile=dockerfile,
        manifest_path=manifest_path,
        output=first,
        private_key_path=key,
        image_tag="course:test",
        command_runner=runner,
    )
    build_plugin_package(
        source_dir=source,
        build_context=context,
        dockerfile=dockerfile,
        manifest_path=manifest_path,
        output=second,
        private_key_path=key,
        image_tag="course:test",
        command_runner=runner,
    )
    assert runner.calls[0] == [
        "docker",
        "build",
        "--file",
        str(dockerfile.resolve()),
        "--tag",
        "course:test",
        str(context.resolve()),
    ]
    assert runner.calls[1][:4] == ["docker", "image", "save", "--output"]
    assert first.read_bytes() == second.read_bytes()

    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["image.tar", "plugin.json", "signature.json"]
        assert all(item.date_time == (2026, 1, 1, 0, 0, 0) for item in archive.infolist())
        assert all((item.external_attr >> 16) & 0o777 == 0o644 for item in archive.infolist())
        manifest = json.loads(archive.read("plugin.json"))
        signature = SignatureEnvelope.model_validate_json(archive.read("signature.json"))
        image = archive.read("image.tar")
    image_digest = "sha256:" + __import__("hashlib").sha256(image).hexdigest()
    assert manifest["image_digest"] == image_digest
    verify_signature(
        signature,
        manifest=manifest,
        image_digest=image_digest,
        package_assets_digest=assets_digest({}),
    )


def test_packager_refuses_overwrite_internal_output_and_internal_key(tmp_path: Path) -> None:
    context = tmp_path / "context"
    source = context / "plugin"
    source.mkdir(parents=True)
    dockerfile = source / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", "utf-8")
    manifest = source / "plugin.json"
    manifest.write_text((COURSE / "plugin.json").read_text("utf-8"), "utf-8")
    external_key = private_key(tmp_path / "external.pem")
    runner = FakeDocker()
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"keep")

    common = {
        "source_dir": source,
        "build_context": context,
        "dockerfile": dockerfile,
        "manifest_path": manifest,
        "private_key_path": external_key,
        "image_tag": "course:test",
        "command_runner": runner,
    }
    with pytest.raises(FileExistsError):
        build_plugin_package(output=existing, **common)
    with pytest.raises(ValueError, match="outside"):
        build_plugin_package(output=source / "inside.zip", **common)
    with pytest.raises(ValueError, match="outside"):
        build_plugin_package(output=context / "context-output.zip", **common)

    internal_key = private_key(source / "private.pem")
    with pytest.raises(ValueError, match="private key"):
        build_plugin_package(
            output=tmp_path / "safe.zip",
            **{**common, "private_key_path": internal_key},
        )
    assert runner.calls == []


@pytest.mark.parametrize(
    "script",
    (
        "backend/scripts/package_diagnostic_plugin.py",
        "backend/scripts/package_course_organizer_plugin.py",
    ),
)
def test_package_wrapper_help_is_side_effect_free(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--private-key" in result.stdout
    assert "--output" in result.stdout


def test_course_wrapper_preserves_installable_package_shape(tmp_path: Path) -> None:
    output = tmp_path / "course.plugin.zip"
    package_course_plugin(
        output=output,
        private_key_path=private_key(tmp_path / "course-key.pem"),
        image_tag="course:test",
        command_runner=FakeDocker(),
    )
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["image.tar", "plugin.json", "signature.json"]
        manifest = PluginManifest.model_validate_json(archive.read("plugin.json"))
    assert manifest.id == "com.matinier.course-organizer"


def test_course_source_capture_is_exact_docker_copy_closure_and_deterministic(
    tmp_path: Path,
) -> None:
    captured = capture_course_plugin_sources()
    names = tuple(captured)

    assert names == tuple(sorted(names))
    assert "plugin-sdk/examples/course-organizer/Dockerfile" in captured
    assert "plugin-sdk/examples/course-organizer/plugin.json" in captured
    assert "plugin-sdk/examples/course-organizer/plugin.py" in captured
    assert "plugin-sdk/examples/course-organizer/course_organizer/session.py" in captured
    assert "plugin-sdk/python/matinier_plugin/runtime.py" in captured
    assert all("__pycache__" not in name and not name.endswith(".pyc") for name in names)
    assert all(
        name.endswith(("Dockerfile", "plugin.json", ".py"))
        for name in names
    )

    snapshot = tmp_path / "snapshot"
    write_course_plugin_snapshot(snapshot, captured)
    for relative, content in captured.items():
        assert (snapshot / relative).read_bytes() == content

    expected = hashlib.sha256()
    for relative, content in captured.items():
        encoded = relative.encode("utf-8")
        expected.update(len(encoded).to_bytes(4, "big"))
        expected.update(encoded)
        expected.update(hashlib.sha256(content).digest())
    assert course_plugin_source_digest(captured) == "sha256:" + expected.hexdigest()


def test_course_source_capture_rejects_symlinked_copy_input(tmp_path: Path) -> None:
    project = tmp_path / "project"
    course = project / "plugin-sdk" / "examples" / "course-organizer"
    package = course / "course_organizer"
    runtime = project / "plugin-sdk" / "python" / "matinier_plugin"
    package.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (course / "Dockerfile").write_text("FROM scratch\n", "utf-8")
    (course / "plugin.json").write_text("{}", "utf-8")
    (course / "plugin.py").write_text("", "utf-8")
    (package / "__init__.py").write_text("", "utf-8")
    (runtime / "__init__.py").write_text("", "utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret", "utf-8")
    try:
        (package / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="links"):
        capture_course_plugin_sources(project_root=project)


def test_course_wrapper_builds_external_snapshot_and_cleans_exact_image(
    tmp_path: Path,
) -> None:
    runner = FakeDocker()
    output = tmp_path / "course.plugin.zip"
    package_course_plugin(
        output=output,
        private_key_path=private_key(tmp_path / "course-key.pem"),
        image_tag="course:unique-test",
        command_runner=runner,
    )

    assert runner.calls[-1] == [
        "docker",
        "image",
        "rm",
        "--force",
        "course:unique-test",
    ]
