from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TranslationEventType(StrEnum):
    STREAM_STARTED = "stream_started"
    PARTIAL_RESULT = "partial_result"
    FINAL_RESULT = "final_result"
    STREAM_COMPLETED = "stream_completed"
    STREAM_ERROR = "stream_error"


class TranslationCaptionStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class TranslationEvent:
    event_type: TranslationEventType
    provider_event_id: str
    target_language: str
    segment_id: str | None = None
    text: str | None = None
    begin_time_ms: int | None = None
    end_time_ms: int | None = None
    raw_payload: dict[str, Any] | None = None
    received_at_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)

    def __post_init__(self) -> None:
        event_type = TranslationEventType(self.event_type)
        object.__setattr__(self, "event_type", event_type)
        if not self.provider_event_id:
            raise ValueError("provider_event_id is required")
        if not self.target_language:
            raise ValueError("target_language is required")

    @property
    def is_final(self) -> bool:
        return self.event_type is TranslationEventType.FINAL_RESULT


@dataclass(slots=True)
class TranslationMetrics:
    final_result_count: int = 0
    provider_error_count: int = 0
    sent_audio_chunk_count: int = 0
    sent_audio_bytes: int = 0

    def observe(self, event: TranslationEvent) -> None:
        if event.event_type is TranslationEventType.FINAL_RESULT:
            self.final_result_count += 1
        elif event.event_type is TranslationEventType.STREAM_ERROR:
            self.provider_error_count += 1

    def mark_audio_sent(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        self.sent_audio_chunk_count += 1
        self.sent_audio_bytes += byte_count

    def log_fields(self) -> dict[str, int]:
        return {
            "translation_final_result_count": self.final_result_count,
            "translation_provider_error_count": self.provider_error_count,
            "translation_sent_audio_chunk_count": self.sent_audio_chunk_count,
            "translation_sent_audio_bytes": self.sent_audio_bytes,
        }


@dataclass(frozen=True, slots=True)
class TranslationCaption:
    session_id: str
    segment_id: str
    revision: int
    status: TranslationCaptionStatus
    text: str
    source_language: str
    target_language: str
    audio_start_ms: int | None
    audio_end_ms: int | None
    source_segment_ids: tuple[str, ...]
    provider_event_id: str
    received_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            TranslationCaptionStatus(self.status),
        )
        if not self.session_id:
            raise ValueError("session_id is required")
        if not self.segment_id:
            raise ValueError("segment_id is required")
        if self.revision <= 0:
            raise ValueError("revision must be positive")

    @property
    def is_final(self) -> bool:
        return self.status is TranslationCaptionStatus.FINAL
