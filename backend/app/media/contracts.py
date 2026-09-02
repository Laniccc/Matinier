from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contract_versions import MEDIA_SCHEMA_VERSION

MediaMode = Literal["live", "playback"]
MediaTrackKind = Literal["audio", "video", "transcript", "chat"]
EventFinality = Literal["draft", "final", "derived"]

MEDIA_EVENT_FAMILIES = {
    "session",
    "playback",
    "transcript",
    "translation",
    "timeline",
    "audio",
    "video",
    "ocr",
    "chat",
    "user",
}

MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
MAX_EVENT_PAYLOAD_DEPTH = 16
_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_SENSITIVE_KEYS = {
    "access_token",
    "accesstoken",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "clientsecret",
    "cookie",
    "password",
    "passwd",
    "private_key",
    "privatekey",
    "secret",
}


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def validate_bounded_json(
    value: Any,
    *,
    max_bytes: int,
    max_depth: int = MAX_EVENT_PAYLOAD_DEPTH,
    reject_sensitive_keys: bool = False,
) -> Any:
    """Validate a JSON boundary without accepting binary or secret-like fields."""

    def walk(item: Any, depth: int) -> None:
        if depth > max_depth:
            raise ValueError("JSON payload exceeds maximum nesting depth")
        if item is None or isinstance(item, (str, int, float, bool)):
            return
        if isinstance(item, bytes | bytearray | memoryview):
            raise ValueError("binary data must use a host-issued blob handle")
        if isinstance(item, list | tuple):
            for child in item:
                walk(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                if reject_sensitive_keys and _normalized_key(key) in _SENSITIVE_KEYS:
                    raise ValueError(f"sensitive-looking payload key is forbidden: {key}")
                walk(child, depth + 1)
            return
        raise ValueError(f"value of type {type(item).__name__} is not JSON-compatible")

    walk(value, 0)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("payload must be canonical JSON-compatible data") from error
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON payload exceeds {max_bytes} bytes")
    return value


class MediaSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MEDIA_SCHEMA_VERSION] = MEDIA_SCHEMA_VERSION
    session_id: str = Field(min_length=1, max_length=128)
    mode: MediaMode
    source_kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    status: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")
    tracks: tuple[MediaTrackKind, ...] = ()
    owner_scope: str | None = Field(default=None, max_length=256)
    created_at: datetime
    updated_at: datetime

    @field_validator("tracks", mode="before")
    @classmethod
    def normalize_tracks(cls, value: object) -> object:
        if isinstance(value, list | tuple | set):
            return tuple(sorted(set(value)))
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class MediaEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MEDIA_SCHEMA_VERSION] = MEDIA_SCHEMA_VERSION
    event_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=3, max_length=96)
    media_time_ms: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    logical_id: str | None = Field(default=None, min_length=1, max_length=256)
    revision: int | None = Field(default=None, ge=1)
    finality: EventFinality
    source: str = Field(min_length=3, max_length=96)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if not _EVENT_TYPE_PATTERN.fullmatch(value):
            raise ValueError("event_type must be a lower-case dotted identifier")
        family = value.split(".", 1)[0]
        if family not in MEDIA_EVENT_FAMILIES:
            raise ValueError(f"unsupported event family: {family}")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if not _SOURCE_PATTERN.fullmatch(value):
            raise ValueError("source must be a lower-case dotted identifier")
        return value

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_bounded_json(
            value,
            max_bytes=MAX_EVENT_PAYLOAD_BYTES,
            reject_sensitive_keys=True,
        )
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_revision_identity(self) -> "MediaEvent":
        if self.revision is not None and self.logical_id is None:
            raise ValueError("logical_id is required when revision is present")
        return self

