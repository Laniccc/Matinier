from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ASREventType(StrEnum):
    STREAM_STARTED = "stream_started"
    PARTIAL_RESULT = "partial_result"
    FINAL_RESULT = "final_result"
    STREAM_COMPLETED = "stream_completed"
    STREAM_ERROR = "stream_error"


@dataclass(frozen=True, slots=True)
class ASREvent:
    """Stable event shape exposed by every speech recognition provider."""

    event_type: ASREventType
    provider_event_id: str
    segment_id: str | None = None
    text: str | None = None
    is_final: bool = False
    begin_time_ms: int | None = None
    end_time_ms: int | None = None
    confidence: float | None = None
    raw_payload: dict[str, Any] | None = None
    received_at_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000)

    def __post_init__(self) -> None:
        event_type = ASREventType(self.event_type)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(
            self,
            "is_final",
            event_type is ASREventType.FINAL_RESULT,
        )


@dataclass(slots=True)
class TranscriptionMetrics:
    """Session-level recognition metrics measured on a monotonic clock."""

    # This timestamp is intentionally unset until the first audio delivery.
    # Keeping the public name preserves the metrics contract while removing
    # LiveKit/HLS connection time from recognition latency.
    started_at_ms: float | None = None
    final_result_count: int = 0
    first_partial_latency_ms: float | None = None
    provider_error_count: int = 0
    sent_audio_chunk_count: int = 0
    sent_audio_bytes: int = 0
    _final_latencies_ms: list[float] = field(default_factory=list, repr=False)

    @property
    def average_final_latency_ms(self) -> float | None:
        if not self._final_latencies_ms:
            return None
        return sum(self._final_latencies_ms) / len(self._final_latencies_ms)

    def observe(
        self,
        event: ASREvent,
        *,
        observed_at_ms: float | None = None,
    ) -> None:
        now_ms = observed_at_ms if observed_at_ms is not None else time.monotonic() * 1_000
        if event.event_type is ASREventType.STREAM_ERROR:
            self.provider_error_count += 1
            return
        if event.event_type not in {
            ASREventType.PARTIAL_RESULT,
            ASREventType.FINAL_RESULT,
        }:
            return
        if event.text is None or not event.text.strip():
            return
        if self.started_at_ms is None:
            return

        elapsed_ms = max(0.0, now_ms - self.started_at_ms)
        if (
            event.event_type is ASREventType.PARTIAL_RESULT
            and self.first_partial_latency_ms is None
        ):
            self.first_partial_latency_ms = elapsed_ms
        elif event.event_type is ASREventType.FINAL_RESULT:
            self.final_result_count += 1
            media_end_ms = float(event.end_time_ms or 0)
            self._final_latencies_ms.append(
                max(0.0, elapsed_ms - media_end_ms)
            )

    def mark_audio_started(self, started_at_ms: float | None = None) -> None:
        if self.started_at_ms is None:
            self.started_at_ms = (
                started_at_ms
                if started_at_ms is not None
                else time.monotonic() * 1_000
            )

    def mark_audio_sent(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        self.mark_audio_started()
        self.sent_audio_chunk_count += 1
        self.sent_audio_bytes += byte_count

    def log_fields(self) -> dict[str, int | float | None]:
        return {
            "final_result_count": self.final_result_count,
            "first_partial_latency_ms": self.first_partial_latency_ms,
            "average_final_latency_ms": self.average_final_latency_ms,
            "provider_error_count": self.provider_error_count,
            "sent_audio_chunk_count": self.sent_audio_chunk_count,
            "sent_audio_bytes": self.sent_audio_bytes,
        }
