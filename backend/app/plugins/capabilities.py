from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.media.contracts import validate_bounded_json
from app.plugins.contracts import CapabilityEffect


class CapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MediaQueryInput(CapabilityInput):
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1_000)
    event_types: tuple[str, ...] = Field(min_length=1, max_length=32)


class MediaQueryOutput(CapabilityOutput):
    events: list[dict[str, object]] = Field(max_length=1_000)

    @field_validator("events")
    @classmethod
    def bounded_events(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        validate_bounded_json(value, max_bytes=512 * 1024)
        return value


class ModelInvokeInput(CapabilityInput):
    input_category: Literal["public", "session_transcript", "plugin_state"]
    system_prompt: str = Field(min_length=1, max_length=32_000)
    user_prompt: str = Field(min_length=1, max_length=32_000)
    input_payload: dict[str, object]
    response_format: Literal["json_object"] = "json_object"
    max_output_tokens: int = Field(ge=1, le=4_096)
    timeout_seconds: float = Field(gt=0, le=30)

    @field_validator("input_payload")
    @classmethod
    def bounded_input_payload(
        cls,
        value: dict[str, object],
    ) -> dict[str, object]:
        validate_bounded_json(
            value,
            max_bytes=192 * 1024,
            reject_sensitive_keys=True,
        )
        return value


class ModelInvokeOutput(CapabilityOutput):
    output: dict[str, object]
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    finish_reason: str | None = Field(default=None, max_length=64)
    output_tokens: int = Field(default=0, ge=0, le=4_096)

    @field_validator("output")
    @classmethod
    def bounded_output(cls, value: dict[str, object]) -> dict[str, object]:
        validate_bounded_json(value, max_bytes=192 * 1024)
        return value


_LANGUAGE_PATTERN = r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"
_DELIVERY_KIND_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class DeliveryPrepareInput(CapabilityInput):
    trigger: Literal[
        "manual",
        "session_completed",
        "session_failed",
        "session_cancelled",
    ]
    output_language: str = Field(
        min_length=2,
        max_length=32,
        pattern=_LANGUAGE_PATTERN,
    )
    final_sequence: int = Field(ge=0)


class DeliveryPrepareOutput(CapabilityOutput):
    package_id: str = Field(min_length=1, max_length=128)
    package_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_language: str = Field(
        min_length=2,
        max_length=32,
        pattern=_LANGUAGE_PATTERN,
    )
    target_languages: tuple[str, ...] = Field(default=(), max_length=32)
    final_sequence: int = Field(ge=0)

    @field_validator("target_languages")
    @classmethod
    def valid_target_languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        pattern = re.compile(_LANGUAGE_PATTERN)
        if any(len(item) > 32 or pattern.fullmatch(item) is None for item in value):
            raise ValueError("invalid target language")
        return value


class DeliveryQueryInput(CapabilityInput):
    package_id: str = Field(min_length=1, max_length=128)
    document_kinds: tuple[str, ...] = Field(min_length=1, max_length=100)
    language: str | None = Field(
        default=None,
        min_length=2,
        max_length=32,
        pattern=_LANGUAGE_PATTERN,
    )
    after_item: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1_000)

    @field_validator("document_kinds")
    @classmethod
    def valid_document_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        pattern = re.compile(_DELIVERY_KIND_PATTERN)
        if any(pattern.fullmatch(item) is None for item in value):
            raise ValueError("invalid delivery document kind")
        if len(set(value)) != len(value):
            raise ValueError("delivery document kinds must be unique")
        return value


class DeliveryQueryOutput(CapabilityOutput):
    package_id: str = Field(min_length=1, max_length=128)
    package_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[dict[str, object], ...] = Field(max_length=1_000)
    next_after_item: int | None = Field(default=None, ge=0)

    @field_validator("items")
    @classmethod
    def bounded_items(
        cls,
        value: tuple[dict[str, object], ...],
    ) -> tuple[dict[str, object], ...]:
        validate_bounded_json(value, max_bytes=192 * 1024)
        return value


class NetworkFetchInput(CapabilityInput):
    url: HttpUrl
    method: Literal["GET", "HEAD", "POST"] = "GET"
    accepted_mime_types: tuple[str, ...] = Field(min_length=1, max_length=16)
    max_response_bytes: int = Field(default=256 * 1024, ge=1, le=2 * 1024 * 1024)
    body: str | None = Field(default=None, max_length=128_000)


class NetworkFetchOutput(CapabilityOutput):
    status: int = Field(ge=100, le=599)
    mime_type: str = Field(min_length=1, max_length=128)
    body: str = Field(max_length=2 * 1024 * 1024)


class StateGetInput(CapabilityInput):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class StateGetOutput(CapabilityOutput):
    value: dict[str, object] | None
    version: int = Field(ge=0)


class StatePutInput(CapabilityInput):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    value: dict[str, object]
    expected_version: int = Field(ge=0)

    @field_validator("value")
    @classmethod
    def bounded_value(cls, value: dict[str, object]) -> dict[str, object]:
        validate_bounded_json(value, max_bytes=512 * 1024)
        return value


class StatePutOutput(CapabilityOutput):
    version: int = Field(ge=1)


class UIViewPublishInput(CapabilityInput):
    surface: Literal["panel", "overlay"]
    view_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    view_version: int = Field(ge=1)
    view: dict[str, object]
    actions: tuple[dict[str, object], ...] = Field(default=(), max_length=32)

    @field_validator("view")
    @classmethod
    def bounded_view(cls, value: dict[str, object]) -> dict[str, object]:
        validate_bounded_json(value, max_bytes=512 * 1024)
        return value


class UIViewPublishOutput(CapabilityOutput):
    accepted: bool
    view_version: int = Field(ge=1)


class ActionExecuteInput(CapabilityInput):
    action: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]+$")
    payload: dict[str, object]

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, value: dict[str, object]) -> dict[str, object]:
        validate_bounded_json(value, max_bytes=256 * 1024)
        return value


class ActionExecuteOutput(CapabilityOutput):
    status: Literal["completed", "rejected"]
    external_id: str | None = Field(default=None, max_length=256)


class CapabilityAdapter(Protocol):
    def __call__(self, context: Any, value: BaseModel) -> Awaitable[BaseModel | dict[str, object]]: ...


CapabilityReconciler = Callable[
    [Any, BaseModel, str],
    Awaitable[BaseModel | dict[str, object]],
]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: str
    version: str
    effect: CapabilityEffect
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    timeout_seconds: float
    requires_action_grant: bool
    supports_idempotency: bool
    supports_reconciliation: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", self.name):
            raise ValueError("invalid capability name")
        if not self.version or self.timeout_seconds <= 0:
            raise ValueError("invalid capability version or timeout")
        if self.requires_action_grant and self.effect != "external_write":
            raise ValueError("action grants are only valid for external writes")
        if self.supports_reconciliation and not self.supports_idempotency:
            raise ValueError("reconciliation requires idempotency")


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    spec: CapabilitySpec
    handler: CapabilityAdapter | None
    reconciler: CapabilityReconciler | None = None


class CapabilityRegistry:
    """Closed registry: only host composition code may add immutable adapters."""

    def __init__(self) -> None:
        self._bindings: dict[str, CapabilityBinding] = {}

    def register_host(
        self,
        spec: CapabilitySpec,
        handler: CapabilityAdapter | None,
        *,
        reconciler: CapabilityReconciler | None = None,
    ) -> None:
        if spec.name in self._bindings:
            raise ValueError(f"capability is already registered: {spec.name}")
        if reconciler is not None and not spec.supports_reconciliation:
            raise ValueError("capability does not declare reconciliation support")
        if spec.supports_reconciliation and reconciler is None:
            raise ValueError("reconcilable capability requires a host reconciler")
        self._bindings[spec.name] = CapabilityBinding(spec, handler, reconciler)

    def register_plugin(self, _spec: CapabilitySpec, _handler: CapabilityAdapter) -> None:
        raise PermissionError("only the host may register capabilities")

    def require(self, name: str) -> CapabilityBinding:
        try:
            return self._bindings[name]
        except KeyError as error:
            raise LookupError(f"unknown capability: {name}") from error


__all__ = [
    "ActionExecuteInput",
    "ActionExecuteOutput",
    "CapabilityBinding",
    "CapabilityRegistry",
    "CapabilitySpec",
    "DeliveryPrepareInput",
    "DeliveryPrepareOutput",
    "DeliveryQueryInput",
    "DeliveryQueryOutput",
    "MediaQueryInput",
    "MediaQueryOutput",
    "ModelInvokeInput",
    "ModelInvokeOutput",
    "NetworkFetchInput",
    "NetworkFetchOutput",
    "StateGetInput",
    "StateGetOutput",
    "StatePutInput",
    "StatePutOutput",
    "UIViewPublishInput",
    "UIViewPublishOutput",
]
