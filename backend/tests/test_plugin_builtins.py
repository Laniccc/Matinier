from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from app.plugins import builtins as builtin_module
from app.plugins.builtins import BuiltinPluginRegistry
from app.plugins.contracts import PluginManifest
from app.settings import PROJECT_ROOT, Settings


def _settings(
    *,
    build_enabled: bool = False,
    framework_enabled: bool = True,
) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://localhost:7880",
        livekit_api_key="key",
        livekit_api_secret="secret",
        livekit_room_name="room",
        plugin_framework_enabled=framework_enabled,
        plugin_builtin_build_enabled=build_enabled,
    )


def test_registry_contains_exact_course_descriptor() -> None:
    registry = BuiltinPluginRegistry(_settings())

    descriptor = registry.require("com.matinier.course-organizer")

    assert descriptor.plugin_id == "com.matinier.course-organizer"
    assert descriptor.display_name == "课程内容整理"
    assert descriptor.description
    assert descriptor.version == "1.0.3"
    assert descriptor.source_id == "course-organizer"
    assert descriptor.permissions == (
        "delivery.prepare",
        "delivery.query",
        "document.publish",
        "model.invoke",
        "state.get",
        "state.put",
        "ui.publish",
    )
    assert descriptor.source_dir == (
        PROJECT_ROOT / "plugin-sdk" / "examples" / "course-organizer"
    ).resolve()
    assert descriptor.source_dir.is_relative_to(PROJECT_ROOT.resolve())
    assert descriptor.dynamic_build_available is False


def test_registry_rejects_unknown_or_path_shaped_ids() -> None:
    registry = BuiltinPluginRegistry(_settings())

    for value in ("unknown", "../course-organizer", "C:\\course"):
        with pytest.raises(LookupError):
            registry.require(value)


def test_registry_descriptor_is_frozen_and_build_availability_comes_from_settings() -> None:
    descriptor = BuiltinPluginRegistry(
        _settings(build_enabled=True)
    ).require("com.matinier.course-organizer")

    assert descriptor.dynamic_build_available is True
    with pytest.raises(FrozenInstanceError):
        descriptor.version = "2.0.0"  # type: ignore[misc]


def test_dynamic_build_is_unavailable_when_plugin_framework_is_disabled() -> None:
    descriptor = BuiltinPluginRegistry(
        _settings(build_enabled=True, framework_enabled=False)
    ).require("com.matinier.course-organizer")

    assert descriptor.dynamic_build_available is False


def test_registry_list_is_deterministic() -> None:
    registry = BuiltinPluginRegistry(_settings())

    assert registry.list() == (
        registry.require("com.matinier.course-organizer"),
        registry.require("com.matinier.meeting-assistant"),
    )


def test_registry_metadata_is_loaded_from_validated_manifest() -> None:
    descriptor = BuiltinPluginRegistry(_settings()).require(
        "com.matinier.course-organizer"
    )
    manifest = PluginManifest.model_validate_json(
        (descriptor.source_dir / "plugin.json").read_bytes()
    )

    assert descriptor.plugin_id == manifest.id
    assert descriptor.display_name == manifest.name
    assert descriptor.version == manifest.version
    assert descriptor.permissions == manifest.permissions


def test_registry_fails_closed_when_manifest_permissions_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source_dir = tmp_path / "course-organizer"
    source_dir.mkdir()
    manifest = json.loads(
        (
            PROJECT_ROOT
            / "plugin-sdk"
            / "examples"
            / "course-organizer"
            / "plugin.json"
        ).read_text(encoding="utf-8")
    )
    manifest["permissions"] = [
        *manifest["permissions"],
        "network.fetch",
    ]
    (source_dir / "plugin.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        builtin_module,
        "COURSE_ORGANIZER_SOURCE_DIR",
        source_dir,
    )

    with pytest.raises(
        ValueError,
        match="course organizer manifest permissions do not match the allowlist",
    ):
        BuiltinPluginRegistry(_settings())
