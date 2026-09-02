from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.sessions.state import SessionStatus


LIVE_CAPTION_TOPIC = "livecaption.events.v1"
LIVE_EVENT_SCHEMA_VERSION = 1

LiveEventTopic = Literal["caption", "translation", "session"]
LiveEventType = Literal[
    "caption.upsert",
    "translation.upsert",
    "translation.status",
    "session.status",
    "session.progress",
    "session.metrics",
    "session.error",
]
class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptionPayload(StrictPayload):
    segment_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    status: Literal["draft", "final"]
    text: str
    audio_start_ms: int | None
    audio_end_ms: int | None
    confidence: float | None
    provider_event_id: str = Field(min_length=1)
    received_at_ms: int = Field(ge=0)


class TranslationPayload(StrictPayload):
    segment_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    status: Literal["draft", "final"]
    text: str
    source_language: str = Field(min_length=2, max_length=32)
    target_language: str = Field(min_length=2, max_length=32)
    audio_start_ms: int | None
    audio_end_ms: int | None
    source_segment_ids: list[str]
    provider_event_id: str = Field(min_length=1)
    received_at_ms: int = Field(ge=0)


class TranslationStatusPayload(StrictPayload):
    status: Literal[
        "starting",
        "running",
        "completed",
        "failed",
    ]
    source_language: str = Field(min_length=2, max_length=32)
    target_language: str = Field(min_length=2, max_length=32)
    error_code: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=1_000)


class SessionStatusPayload(StrictPayload):
    status: SessionStatus


class SessionProgressPayload(StrictPayload):
    audio_time_ms: int = Field(ge=0)


class SessionMetricsPayload(StrictPayload):
    final_result_count: int = Field(ge=0)
    first_partial_latency_ms: float | None = Field(default=None, ge=0)
    average_final_latency_ms: float | None = Field(default=None, ge=0)
    provider_error_count: int = Field(ge=0)
    sent_audio_chunk_count: int = Field(ge=0)
    sent_audio_bytes: int = Field(ge=0)


class SessionErrorPayload(StrictPayload):
    error_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000)


class LiveEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = LIVE_EVENT_SCHEMA_VERSION
    topic: LiveEventTopic
    type: LiveEventType
    session_id: str = Field(min_length=1)
    sent_at_ms: int = Field(ge=0)
    payload: dict[str, object]
