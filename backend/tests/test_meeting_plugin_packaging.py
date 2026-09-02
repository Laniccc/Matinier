import asyncio
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.plugins.builtins import BuiltinPluginRegistry
from app.plugins.builtin_packages import BuiltinPluginPackageService
from app.plugins.contracts import PluginManifest
from app.settings import PROJECT_ROOT
from scripts.builtin_plugin_sources import require_builtin_source
from scripts.package_meeting_assistant_plugin import package_meeting_plugin
from test_course_plugin_packaging import private_key, FakeDocker
from test_builtin_plugin_packages import _settings


MEETING_ID = "com.matinier.meeting-assistant"


def test_two_closed_sources_and_meeting_manifest(tmp_path):
    registry = BuiltinPluginRegistry(_settings(tmp_path / "builtin-work"))
    assert {d.plugin_id for d in registry.list()} == {MEETING_ID, "com.matinier.course-organizer"}
    descriptor = registry.require(MEETING_ID)
    manifest = PluginManifest.model_validate_json((descriptor.source_dir / "plugin.json").read_bytes())
    assert manifest.permissions == descriptor.permissions
    assert manifest.version == "1.0.0"
    assert set(manifest.permissions) == {
        "meeting.state.query", "meeting.operation.query", "meeting.turn.submit", "meeting.mark.write",
        "meeting.execution.submit", "meeting.execution.input", "meeting.execution.cancel",
        "state.get", "state.put", "ui.publish",
    }
    assert not {"network.fetch", "model.invoke", "action.execute"} & set(manifest.permissions)
    with pytest.raises(LookupError):
        require_builtin_source("../meeting-assistant")


def test_meeting_permissions_drift_and_snapshot_escape_fail_closed(tmp_path, monkeypatch):
    from app.plugins import builtins
    source = require_builtin_source("meeting-assistant")
    manifest = json.loads((PROJECT_ROOT / source.relative_root / "plugin.json").read_bytes())
    manifest["permissions"].append("network.fetch")
    (tmp_path / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(builtins, "MEETING_ASSISTANT_SOURCE_DIR", tmp_path)
    with pytest.raises(ValueError, match="permissions do not match"):
        BuiltinPluginRegistry(_settings(tmp_path / "unused"))
    for invalid in ("../key.pem", "plugin-sdk/examples/course-organizer/plugin.py", "plugin-sdk/examples/meeting-assistant/.env"):
        with pytest.raises(ValueError):
            source.write_snapshot(tmp_path / "snapshot", {invalid: b"forbidden"})
        assert not (tmp_path / "snapshot").exists()


def test_source_closures_and_digests_are_independent(tmp_path):
    course = require_builtin_source("course-organizer")
    meeting = require_builtin_source("meeting-assistant")
    course_bytes, meeting_bytes = course.capture(), meeting.capture()
    assert not any("meeting-assistant" in name for name in course_bytes)
    assert not any("course-organizer" in name for name in meeting_bytes)
    assert all("__pycache__" not in name and name.endswith((".py", "Dockerfile", "plugin.json")) for name in meeting_bytes)
    original_digest = course.digest(course_bytes)
    meeting.write_snapshot(tmp_path / "meeting", meeting_bytes)
    assert meeting.capture(project_root=tmp_path / "meeting") == meeting_bytes
    assert course.digest(course.capture()) == original_digest
    dockerfile = meeting_bytes["plugin-sdk/examples/meeting-assistant/Dockerfile"].decode()
    assert "USER plugin" in dockerfile and "COPY ." not in dockerfile
    assert "pip install" not in dockerfile
    assert "plugin-sdk/examples/meeting-assistant/meeting_assistant" in dockerfile


def test_meeting_wrapper_signs_standard_package_and_owns_cleanup(tmp_path):
    runner = FakeDocker()
    output = tmp_path / "meeting.plugin.zip"
    package_meeting_plugin(output=output, private_key_path=private_key(tmp_path / "test-key.pem"),
        image_tag="meeting:unique-test", command_runner=runner)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["image.tar", "plugin.json", "signature.json"]
        assert json.loads(archive.read("plugin.json"))["id"] == MEETING_ID
    assert runner.calls[-1] == ["docker", "image", "rm", "--force", "meeting:unique-test"]
    assert not list(tmp_path.glob("matinier-meeting-source-*"))


def test_meeting_wrapper_help_does_not_build():
    result = subprocess.run([sys.executable, str(PROJECT_ROOT / "backend/scripts/package_meeting_assistant_plugin.py"), "--help"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert all(arg in result.stdout for arg in ("--private-key", "--output", "--image-tag"))


def test_two_plugin_builds_have_separate_snapshots_and_cache(tmp_path):
    async def scenario():
        calls = []
        def build(**kwargs):
            context = Path(kwargs["build_context"])
            manifests = list(context.glob("plugin-sdk/examples/*/plugin.json"))
            assert len(manifests) == 1
            source_id = manifests[0].parent.name
            calls.append(source_id)
            require_builtin_source(source_id).build(**kwargs, command_runner=FakeDocker())
        settings = _settings(tmp_path / "builtin-work")
        service = BuiltinPluginPackageService(settings=settings, registry=BuiltinPluginRegistry(settings), builder=build)
        course, meeting = await asyncio.gather(service.prepare("com.matinier.course-organizer"), service.prepare(MEETING_ID))
        assert course != meeting and set(calls) == {"course-organizer", "meeting-assistant"}
        assert await service.prepare(MEETING_ID) == meeting
        assert len(calls) == 2
        meeting.write_bytes(b"corrupt")
        assert await service.prepare(MEETING_ID) == meeting
        assert await service.prepare("com.matinier.course-organizer") == course
        assert calls.count("course-organizer") == 1 and calls.count("meeting-assistant") == 2
        with pytest.raises(LookupError):
            await service.prepare("../unknown")
        assert len(calls) == 3
    asyncio.run(scenario())


def test_real_meeting_smoke_is_isolated_and_uses_only_owned_cleanup():
    source = (PROJECT_ROOT / "backend/smoke/meeting_assistant_plugin_smoke.py").read_text("utf-8")
    assert "AcceptanceSettings" not in source  # settings are composed by the shared bounded harness
    assert 'real_docker=True' in source and 'OwnedDocker' in source
    assert 'NetworkMode' in source and 'ReadonlyRootfs' in source and 'not config.get("Binds")' in source
    assert 'external_create_count' in source and 'h.linear.create_calls == 1' in source
    assert 'docker("container", "rm", "--force", identifier)' in source
    assert 'docker("image", "rm", "--force", tag)' in source
    assert not any(token in source for token in ("docker system prune", "docker volume", "factory reset"))
