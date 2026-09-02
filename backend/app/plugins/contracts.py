from __future__ import annotations

import re
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.media.contracts import MEDIA_EVENT_FAMILIES, validate_bounded_json

PluginLifecycleStatus = Literal[
    "installed",
    "disabled",
    "starting",
    "ready",
    "degraded",
    "crashed",
    "quarantined",
    "incompatible",
]
CapabilityEffect = Literal["read", "local_write", "network", "external_write"]
RpcId: TypeAlias = str | int

ERROR_PROTOCOL_INVALID = "plugin.protocol.invalid"
ERROR_PERMISSION_DENIED = "plugin.permission.denied"
ERROR_METHOD_NOT_FOUND = "plugin.protocol.method_not_found"
ERROR_REQUEST_TIMEOUT = "plugin.protocol.timeout"
ERROR_SCOPE_INVALID = "plugin.scope.invalid"
ERROR_CAPABILITY_FAILED = "plugin.capability.failed"

PUBLIC_ERROR_CODES = frozenset(
    {
        ERROR_PROTOCOL_INVALID,
        ERROR_PERMISSION_DENIED,
        ERROR_METHOD_NOT_FOUND,
        ERROR_REQUEST_TIMEOUT,
        ERROR_SCOPE_INVALID,
        ERROR_CAPABILITY_FAILED,
    }
)

MAX_RPC_MESSAGE_BYTES = 256 * 1024
_PLUGIN_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
    r"(?:\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*){2,}$"
)
_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_HOST_RANGE_TOKEN = re.compile(
    r"^(?:>=|<=|>|<|=|\^|~)?(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){1,2}$"
)
_DOTTED_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _normalize_unique(
    value: object,
    *,
    declaration: str,
    pattern: re.Pattern[str],
    event_family: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list | tuple | set):
        raise ValueError(f"{declaration} must be a list")
    normalized = tuple(str(item).strip().casefold() for item in value)
    if any(not item for item in normalized):
        raise ValueError(f"{declaration} cannot contain empty values")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"duplicate {declaration} declaration")
    for item in normalized:
        if not pattern.fullmatch(item):
            raise ValueError(f"invalid {declaration} declaration: {item}")
        if event_family and item.split(".", 1)[0] not in MEDIA_EVENT_FAMILIES:
            raise ValueError(f"unsupported event family in {declaration}: {item}")
    return tuple(sorted(normalized))


def _validate_rpc_id(value: RpcId | None) -> RpcId | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("JSON-RPC id cannot be a boolean")
    if isinstance(value, str):
        if not value or len(value.encode("utf-8")) > 128:
            raise ValueError("JSON-RPC string id must be between 1 and 128 bytes")
    elif not isinstance(value, int):
        raise ValueError("JSON-RPC id must be a string, integer, or null")
    return value


class PluginResourceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_mb: int = Field(default=256, ge=64, le=4_096)
    cpu_count: float = Field(default=0.5, gt=0, le=4)
    pids: int = Field(default=64, ge=16, le=512)
    tmpfs_mb: int = Field(default=64, ge=16, le=1_024)


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    id: str = Field(min_length=5, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=5, max_length=64)
    publisher: str = Field(min_length=1, max_length=128)
    host_api: str = Field(min_length=3, max_length=128)
    image_digest: str
    subscriptions: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    resources: PluginResourceLimits = Field(default_factory=PluginResourceLimits)
    ui_schema_version: int = Field(default=1, ge=1)
    state_schema_version: int = Field(default=1, ge=1)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PLUGIN_ID_PATTERN.fullmatch(value):
            raise ValueError("plugin id must be a lower-case reverse-domain identifier")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _SEMVER_PATTERN.fullmatch(value):
            raise ValueError("version must use semantic versioning")
        return value

    @field_validator("host_api")
    @classmethod
    def normalize_host_api(cls, value: str) -> str:
        tokens = tuple(part.strip() for part in value.replace(",", " ").split())
        if not tokens or any(not _HOST_RANGE_TOKEN.fullmatch(token) for token in tokens):
            raise ValueError("host_api must be a whitespace-separated semantic-version range")
        return " ".join(tokens)

    @field_validator("image_digest")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _IMAGE_DIGEST.fullmatch(normalized):
            raise ValueError("image_digest must be a lowercase sha256 digest")
        return normalized

    @field_validator("subscriptions", mode="before")
    @classmethod
    def normalize_subscriptions(cls, value: object) -> tuple[str, ...]:
        return _normalize_unique(
            value,
            declaration="subscription",
            pattern=_DOTTED_NAME,
            event_family=True,
        )

    @field_validator("permissions", mode="before")
    @classmethod
    def normalize_permissions(cls, value: object) -> tuple[str, ...]:
        return _normalize_unique(value, declaration="permission", pattern=_DOTTED_NAME)

    @field_validator("commands", mode="before")
    @classmethod
    def normalize_commands(cls, value: object) -> tuple[str, ...]:
        return _normalize_unique(value, declaration="command", pattern=_COMMAND_NAME)


class SessionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=16, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    jsonrpc: Literal["2.0"] = "2.0"
    id: RpcId
    method: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$",
    )
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: RpcId) -> RpcId:
        validated = _validate_rpc_id(value)
        assert validated is not None
        return validated

    @field_validator("params")
    @classmethod
    def validate_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_bounded_json(value, max_bytes=MAX_RPC_MESSAGE_BYTES)
        return value


class JsonRpcResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    jsonrpc: Literal["2.0"] = "2.0"
    id: RpcId
    result: Any

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: RpcId) -> RpcId:
        validated = _validate_rpc_id(value)
        assert validated is not None
        return validated

    @field_validator("result")
    @classmethod
    def validate_result(cls, value: Any) -> Any:
        validate_bounded_json(value, max_bytes=MAX_RPC_MESSAGE_BYTES)
        return value


class JsonRpcError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str = Field(min_length=1, max_length=512)
    data: dict[str, Any] | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if value not in PUBLIC_ERROR_CODES:
            raise ValueError("error code is not part of the stable public contract")
        return value

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        validate_bounded_json(value, max_bytes=MAX_RPC_MESSAGE_BYTES)
        return value


class JsonRpcErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    jsonrpc: Literal["2.0"] = "2.0"
    id: RpcId | None
    error: JsonRpcError

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: RpcId | None) -> RpcId | None:
        return _validate_rpc_id(value)

