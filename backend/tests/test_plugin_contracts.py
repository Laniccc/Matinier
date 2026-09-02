from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contract_versions import (
    CAPABILITY_API_VERSION,
    HOST_API_VERSION,
    MEDIA_SCHEMA_VERSION,
    PLUGIN_RPC_VERSION,
    UI_SCHEMA_VERSION,
)
from app.plugins.contracts import (
    ERROR_PERMISSION_DENIED,
    ERROR_PROTOCOL_INVALID,
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcResponse,
    PluginManifest,
    PluginResourceLimits,
    SessionScope,
)
from app.settings import Settings


def manifest(**overrides: object) -> PluginManifest:
    values: dict[str, object] = {
        "schema_version": 1,
        "id": "com.example.learning-assistant",
        "name": "Learning Assistant",
        "version": "1.0.0",
        "publisher": "Example",
        "host_api": ">=1.0 <2.0",
        "image_digest": "sha256:" + "a" * 64,
        "subscriptions": ["transcript.final", "session.completed"],
        "permissions": [
            "media.transcript.final.read",
            "model.invoke",
            "plugin_state.write",
            "ui.publish",
        ],
        "commands": ["summarize", "explain"],
        "resources": {"memory_mb": 256, "cpu_count": 0.5, "pids": 64, "tmpfs_mb": 64},
        "ui_schema_version": 1,
        "state_schema_version": 1,
    }
    values.update(overrides)
    return PluginManifest.model_validate(values)


def test_contract_versions_are_independent_and_frozen() -> None:
    assert HOST_API_VERSION == "1.0.0"
    assert MEDIA_SCHEMA_VERSION == 1
    assert PLUGIN_RPC_VERSION == 1
    assert UI_SCHEMA_VERSION == 1
    assert CAPABILITY_API_VERSION == 1


def test_manifest_normalizes_declarations_and_resource_limits() -> None:
    parsed = manifest(
        subscriptions=[" transcript.final ", "session.completed"],
        permissions=[" UI.PUBLISH ", "model.invoke"],
        commands=[" Summarize ", "explain"],
        resources={"memory_mb": 256, "cpu_count": "0.50", "pids": 64, "tmpfs_mb": 64},
    )
    assert parsed.id == "com.example.learning-assistant"
    assert parsed.subscriptions == ("session.completed", "transcript.final")
    assert parsed.permissions == ("model.invoke", "ui.publish")
    assert parsed.commands == ("explain", "summarize")
    assert parsed.resources == PluginResourceLimits(
        memory_mb=256,
        cpu_count=0.5,
        pids=64,
        tmpfs_mb=64,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "learning-assistant"),
        ("id", "Com.Example.Plugin"),
        ("version", "v1"),
        ("version", "1.0"),
        ("host_api", "latest"),
        ("host_api", ">=1.0 || <2"),
    ],
)
def test_manifest_rejects_invalid_identity_and_versions(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        manifest(**{field: value})


@pytest.mark.parametrize("field", ["subscriptions", "permissions", "commands"])
def test_manifest_rejects_duplicate_declarations_after_normalization(field: str) -> None:
    duplicated = {
        "subscriptions": ["transcript.final", " transcript.final "],
        "permissions": ["ui.publish", " UI.PUBLISH "],
        "commands": ["summarize", " Summarize "],
    }
    with pytest.raises(ValidationError):
        manifest(**{field: duplicated[field]})


def test_session_scope_is_an_opaque_bounded_host_value() -> None:
    assert SessionScope(value="scope_7Yax0pG_opaque").value == "scope_7Yax0pG_opaque"
    with pytest.raises(ValidationError):
        SessionScope(value="short")
    with pytest.raises(ValidationError):
        SessionScope(value="x" * 257)


def test_json_rpc_envelopes_are_bounded_and_mutually_exclusive() -> None:
    request = JsonRpcRequest(id="request-1", method="session.open", params={"scope": "opaque"})
    assert request.jsonrpc == "2.0"
    assert JsonRpcRequest.model_validate_json(request.model_dump_json()) == request

    response = JsonRpcResponse(id="request-1", result={"ready": True})
    error = JsonRpcErrorResponse(
        id="request-1",
        error=JsonRpcError(code=ERROR_PROTOCOL_INVALID, message="invalid envelope"),
    )
    assert response.result == {"ready": True}
    assert error.error.code == "plugin.protocol.invalid"

    with pytest.raises(ValidationError):
        JsonRpcRequest(id="x" * 129, method="session.open", params={})
    with pytest.raises(ValidationError):
        JsonRpcRequest(id="request-1", method="session.open", params={"x": "y" * 270_000})


def test_public_error_codes_are_stable_namespaced_values() -> None:
    assert ERROR_PROTOCOL_INVALID == "plugin.protocol.invalid"
    assert ERROR_PERMISSION_DENIED == "plugin.permission.denied"
    with pytest.raises(ValidationError):
        JsonRpcError(code="Permission denied", message="bad")


def test_plugin_settings_create_isolated_persistent_directories(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        data_dir=tmp_path / "data",
        database_url="sqlite://",
    )

    assert settings.plugin_framework_enabled is True
    assert settings.plugin_allow_unsigned is False
    assert settings.plugin_container_runtime == "docker"
    assert settings.plugin_admin_token is None
    assert {
        settings.plugin_packages_dir,
        settings.plugin_images_dir,
        settings.plugin_state_dir,
        settings.plugin_staging_dir,
        settings.plugin_audit_dir,
    }.issubset(set(settings.persistent_directories))
    assert all(path.is_dir() for path in settings.persistent_directories)
