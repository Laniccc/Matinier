from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.translation.errors import (
    TranslationProviderError,
    TranslationStateError,
    TranslationTimeoutError,
)
from app.translation.models import TranslationEvent, TranslationEventType
from app.translation.provider import SpeechTranslationProvider


_EVENTS_DONE = object()


@dataclass(frozen=True, slots=True)
class FakeTranslationConfig:
    target_language: str
    chunks_per_segment: int = 3
    send_delay_seconds: float = 0.0
    emit_duplicate_final: bool = False
    emit_stale_partial: bool = False
    fail_after_chunks: int | None = None
    timeout_on_finish: bool = False

    def __post_init__(self) -> None:
        if not self.target_language:
            raise ValueError("target_language is required")
        if self.chunks_per_segment < 3:
            raise ValueError("chunks_per_segment must be at least 3")
        if self.send_delay_seconds < 0:
            raise ValueError("send_delay_seconds must be non-negative")
        if self.fail_after_chunks is not None and self.fail_after_chunks <= 0:
            raise ValueError("fail_after_chunks must be positive")


class FakeTranslationProvider(SpeechTranslationProvider):
    """Deterministic no-cloud translation provider for full route tests."""

    def __init__(self, config: FakeTranslationConfig) -> None:
        self.config = config
        self.audio_chunk_count = 0
        self.audio_bytes = 0
        self._segment_number = 1
        self._event_number = 0
        self._state = "idle"
        self._closed = False
        self._events: asyncio.Queue[TranslationEvent | object] = asyncio.Queue()

    async def start(self) -> None:
        if self._state != "idle":
            raise TranslationStateError(
                "fake translation provider can only be started once"
            )
        self._state = "started"
        await self._emit(TranslationEventType.STREAM_STARTED)

    async def send_audio(self, pcm: bytes) -> None:
        if self._state != "started":
            raise TranslationStateError(
                "fake translation provider is not accepting audio"
            )
        if self.config.send_delay_seconds:
            await asyncio.sleep(self.config.send_delay_seconds)
        self.audio_chunk_count += 1
        self.audio_bytes += len(pcm)
        if self.audio_chunk_count == self.config.fail_after_chunks:
            raise TranslationProviderError("injected fake translation failure")

        phase = (self.audio_chunk_count - 1) % self.config.chunks_per_segment
        if phase == 0:
            await self._emit_result(TranslationEventType.PARTIAL_RESULT, "v1")
        elif phase == self.config.chunks_per_segment - 2:
            await self._emit_result(TranslationEventType.PARTIAL_RESULT, "v2")
        elif phase == self.config.chunks_per_segment - 1:
            await self._finish_segment()

    async def finish(self) -> None:
        if self._state == "completed":
            return
        if self._state != "started":
            raise TranslationStateError(
                "fake translation provider cannot finish in this state"
            )
        if self.config.timeout_on_finish:
            raise TranslationTimeoutError(
                "injected fake translation finish timeout"
            )

        remainder = self.audio_chunk_count % self.config.chunks_per_segment
        if remainder:
            if remainder == 1:
                await self._emit_result(
                    TranslationEventType.PARTIAL_RESULT,
                    "v2",
                )
            await self._finish_segment()
        await self._emit(TranslationEventType.STREAM_COMPLETED)
        self._state = "completed"
        await self._events.put(_EVENTS_DONE)

    async def events(self) -> AsyncIterator[TranslationEvent]:
        while True:
            event = await self._events.get()
            if event is _EVENTS_DONE:
                return
            assert isinstance(event, TranslationEvent)
            yield event

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._state != "completed":
            self._state = "closed"
        await self._events.put(_EVENTS_DONE)

    async def _finish_segment(self) -> None:
        await self._emit_result(TranslationEventType.FINAL_RESULT, "v3")
        if self.config.emit_duplicate_final:
            await self._emit_result(TranslationEventType.FINAL_RESULT, "v3")
        if self.config.emit_stale_partial:
            await self._emit_result(
                TranslationEventType.PARTIAL_RESULT,
                "stale-v1",
            )
        self._segment_number += 1

    async def _emit_result(
        self,
        event_type: TranslationEventType,
        version: str,
    ) -> None:
        segment_id = f"fake-translation-{self._segment_number:06d}"
        end_ms = self.audio_chunk_count * 100
        start_ms = max(
            0,
            end_ms - self.config.chunks_per_segment * 100,
        )
        await self._emit(
            event_type,
            segment_id=segment_id,
            text=f"fake translation {self._segment_number} {version}",
            begin_time_ms=start_ms,
            end_time_ms=end_ms,
        )

    async def _emit(
        self,
        event_type: TranslationEventType,
        **fields,
    ) -> None:
        self._event_number += 1
        await self._events.put(
            TranslationEvent(
                event_type=event_type,
                provider_event_id=(
                    f"fake-translation-event-{self._event_number}"
                ),
                target_language=self.config.target_language,
                raw_payload={"provider": "fake"},
                **fields,
            )
        )

