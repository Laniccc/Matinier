from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.assistant.models import ActionGrant, ToolEffect
from app.assistant.state_machine import ExecutionProfile


ToolResultStatus = Literal["succeeded", "pending", "failed", "unknown"]
_SENSITIVE_REFERENCE_PARTS = (
    "authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "cookie",
)


def _bounded_json(value: object, *, field_name: str, limit: int) -> object:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > limit:
        raise ValueError(f"{field_name} exceeds its persisted size limit")
    return value


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9_]", "", str(key).casefold())
            if any(part in normalized for part in _SENSITIVE_REFERENCE_PARTS):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


class FrozenToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolInvocation(FrozenToolModel):
    execution_id: str = Field(min_length=1, max_length=36)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    grant_id: str | None = Field(default=None, max_length=36)
    candidate_id: str | None = Field(default=None, max_length=36)
    requested_resource_scope: dict[str, Any] = Field(default_factory=dict)
    logical_action_key: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=255)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    step_id: str | None = Field(default=None, max_length=36)
    prepared_tool_call_id: str | None = Field(default=None, max_length=36)

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tool evidence references must be unique")
        return value

    @field_validator("arguments", "requested_resource_scope")
    @classmethod
    def bounded_inputs(cls, value: dict[str, Any], info) -> dict[str, Any]:
        return dict(
            _bounded_json(value, field_name=info.field_name, limit=32_000)
        )


class ToolResult(FrozenToolModel):
    status: ToolResultStatus
    output: dict[str, Any] | None = None
    external_reference: dict[str, Any] | None = None
    confirmed_side_effects: int | None = Field(default=None, ge=0, le=1)
    error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9_.-]+$",
    )
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    retryable: bool = False

    @field_validator("error_message")
    @classmethod
    def normalize_error_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("tool error message must not be blank")
        return normalized

    @field_validator("output")
    @classmethod
    def bounded_output(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return dict(_bounded_json(value, field_name="output", limit=64_000))

    @field_validator("external_reference")
    @classmethod
    def safe_external_reference(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if _contains_sensitive_key(value):
            raise ValueError("external reference contains a sensitive field")
        return dict(
            _bounded_json(
                value,
                field_name="external_reference",
                limit=8_000,
            )
        )

    @model_validator(mode="after")
    def validate_status_payload(self) -> ToolResult:
        has_error = self.error_code is not None or self.error_message is not None
        if self.status == "succeeded":
            if self.output is None:
                raise ValueError("succeeded tool results require output")
            if has_error or self.retryable:
                raise ValueError("succeeded tool results cannot contain an error")
        elif self.status in {"failed", "unknown"}:
            if self.error_code is None or self.error_message is None:
                raise ValueError("failed and unknown results require a safe error")
            if self.output is not None:
                raise ValueError("failed and unknown results cannot contain output")
            if self.status == "unknown" and self.retryable:
                raise ValueError("unknown outcomes cannot be retried directly")
        elif has_error or self.retryable:
            raise ValueError("pending tool results cannot contain an error")
        if self.status in {"pending", "unknown"} and self.confirmed_side_effects is not None:
            raise ValueError(
                "pending and unknown results cannot confirm a side-effect count"
            )
        return self


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    version: str
    capability: str
    effect: ToolEffect
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    timeout_seconds: float
    supports_idempotency: bool
    supports_reconciliation: bool
    allowed_profiles: frozenset[ExecutionProfile] = frozenset()

    def __post_init__(self) -> None:
        for field_name in ("name", "version", "capability"):
            value = getattr(self, field_name)
            if not value.strip():
                raise ValueError(f"ToolSpec {field_name} is required")
        if len(self.name) > 128 or len(self.version) > 32 or len(self.capability) > 128:
            raise ValueError("ToolSpec identifier exceeds its persisted limit")
        if self.effect not in {"read", "local_write", "external_write"}:
            raise ValueError("ToolSpec effect is invalid")
        if not isinstance(self.input_model, type) or not issubclass(
            self.input_model,
            BaseModel,
        ):
            raise TypeError("ToolSpec input_model must be a Pydantic model")
        if not isinstance(self.output_model, type) or not issubclass(
            self.output_model,
            BaseModel,
        ):
            raise TypeError("ToolSpec output_model must be a Pydantic model")
        if self.input_model.model_config.get("extra") != "forbid":
            raise ValueError("tool input models must forbid extra fields")
        if self.output_model.model_config.get("extra") != "forbid":
            raise ValueError("tool output models must forbid extra fields")
        if self.timeout_seconds <= 0:
            raise ValueError("ToolSpec timeout_seconds must be positive")
        if self.effect == "external_write" and not (
            self.supports_idempotency and self.supports_reconciliation
        ):
            raise ValueError(
                "external-write tools require idempotency and reconciliation"
            )
        profiles = self.allowed_profiles or (
            frozenset({"action_run"})
            if self.effect == "external_write"
            else frozenset({"fast_turn", "action_run"})
        )
        if not profiles or not profiles <= {"fast_turn", "action_run"}:
            raise ValueError("ToolSpec allowed_profiles is invalid")
        if self.effect == "external_write":
            profiles = frozenset({"action_run"})
        object.__setattr__(self, "allowed_profiles", frozenset(profiles))


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    execution_id: str
    session_id: str
    tool_call_id: str
    arguments: BaseModel
    grant: ActionGrant | None
    candidate_id: str | None
    logical_action_key: str | None
    idempotency_key: str | None
    evidence_refs: tuple[str, ...]
    external_reference: dict[str, Any] | None
    attempt: int
    reconciling: bool = False


@runtime_checkable
class ToolAdapter(Protocol):
    @property
    def provider_name(self) -> str: ...

    async def execute(self, context: ToolExecutionContext) -> ToolResult: ...

    async def reconcile(self, context: ToolExecutionContext) -> ToolResult: ...


__all__ = [
    "ToolAdapter",
    "ToolExecutionContext",
    "ToolInvocation",
    "ToolResult",
    "ToolResultStatus",
    "ToolSpec",
]
