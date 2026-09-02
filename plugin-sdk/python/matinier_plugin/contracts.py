from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


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


class PluginRuntimeClosedError(ConnectionError):
    """The Host transport closed before an outstanding call completed."""


@dataclass(frozen=True, slots=True)
class PluginRemoteError(RuntimeError):
    code: str
    message: str
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)


class PluginError(RuntimeError):
    """A deliberately public error that a plugin handler may return to Host."""

    def __init__(
        self,
        code: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        if code not in PUBLIC_ERROR_CODES:
            raise ValueError("error code is not part of the public plugin contract")
        if not message or len(message) > 512:
            raise ValueError("public error message must contain 1 to 512 characters")
        validate_json_value(data)
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


def validate_rpc_id(value: object) -> str | int:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError("invalid JSON-RPC ID")
    if isinstance(value, str) and (not value or len(value) > 128):
        raise ValueError("invalid JSON-RPC ID")
    return value


def validate_json_value(value: object) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("value is not bounded JSON") from error
    if len(encoded) > MAX_RPC_MESSAGE_BYTES:
        raise ValueError("JSON-RPC message exceeds the byte limit")


__all__ = [
    "ERROR_CAPABILITY_FAILED",
    "ERROR_METHOD_NOT_FOUND",
    "ERROR_PERMISSION_DENIED",
    "ERROR_PROTOCOL_INVALID",
    "ERROR_REQUEST_TIMEOUT",
    "ERROR_SCOPE_INVALID",
    "MAX_RPC_MESSAGE_BYTES",
    "PUBLIC_ERROR_CODES",
    "PluginError",
    "PluginRemoteError",
    "PluginRuntimeClosedError",
    "validate_json_value",
    "validate_rpc_id",
]
