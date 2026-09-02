from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.transcription.errors import (
    ASRProviderError,
    ASRStateError,
    ASRTimeoutError,
)
from app.transcription.models import ASREvent, ASREventType
from app.transcription.provider import SpeechRecognitionProvider


_EVENTS_DONE = object()


@dataclass(frozen=True, slots=True)
class FakeASRConfig:
    chunks_per_segment: int = 3
    send_delay_seconds: float = 0.0
    emit_duplicate_final: bool = False
    emit_stale_partial: bool = False
    fail_after_chunks: int | None = None
    timeout_on_finish: bool = False

    def __post_init__(self) -> None:
        if self.chunks_per_segment < 3:
            raise ValueError("chunks_per_segment must be at least 3")
        if self.send_delay_seconds < 0:
            raise ValueError("send_delay_seconds must be non-negative")
        if self.fail_after_chunks is not None and self.fail_after_chunks <= 0:
            raise ValueError("fail_after_chunks must be positive")


class FakeASRProvider(SpeechRecognitionProvider):
    """Deterministic no-cloud ASR provider for lifecycle and failure tests."""

    def __init__(self, config: FakeASRConfig | None = None) -> None:
        self.config = config or FakeASRConfig()
        self.audio_chunk_count = 0
        self.audio_bytes = 0
        self._segment_number = 1
        self._event_number = 0
        self._state = "idle"
        self._closed = False
        self._events: asyncio.Queue[ASREvent | object] = asyncio.Queue()

    async def start(self) -> None:
        if self._state != "idle":
            raise ASRStateError("fake ASR provider can only be started once")
        self._state = "started"
        await self._emit(ASREventType.STREAM_STARTED)

    async def send_audio(self, pcm: bytes) -> None:
        if self._state != "started":
            raise ASRStateError("fake ASR provider is not accepting audio")
        if self.config.send_delay_seconds:
            await asyncio.sleep(self.config.send_delay_seconds)
        self.audio_chunk_count += 1
        self.audio_bytes += len(pcm)
        if self.audio_chunk_count == self.config.fail_after_chunks:
            raise ASRProviderError("injected fake ASR failure")

        phase = (self.audio_chunk_count - 1) % self.config.chunks_per_segment
        if phase == 0:
            await self._emit_result(ASREventType.PARTIAL_RESULT, "v1")
        elif phase == self.config.chunks_per_segment - 2:
            await self._emit_result(ASREventType.PARTIAL_RESULT, "v2")
        elif phase == self.config.chunks_per_segment - 1:
            await self._finish_segment()

    async def finish(self) -> None:
        if self._state == "completed":
            return
        if self._state != "started":
            raise ASRStateError("fake ASR provider cannot finish in this state")
        if self.config.timeout_on_finish:
            raise ASRTimeoutError("injected fake ASR finish timeout")

        remainder = self.audio_chunk_count % self.config.chunks_per_segment
        if remainder:
            if remainder == 1:
                await self._emit_result(ASREventType.PARTIAL_RESULT, "v2")
            await self._finish_segment()
        await self._emit(ASREventType.STREAM_COMPLETED)
        self._state = "completed"
        await self._events.put(_EVENTS_DONE)

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            event = await self._events.get()
            if event is _EVENTS_DONE:
                return
            assert isinstance(event, ASREvent)
            yield event

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._state != "completed":
            self._state = "closed"
        await self._events.put(_EVENTS_DONE)

    async def _finish_segment(self) -> None:
        await self._emit_result(ASREventType.FINAL_RESULT, "v3")
        if self.config.emit_duplicate_final:
            await self._emit_result(ASREventType.FINAL_RESULT, "v3")
        if self.config.emit_stale_partial:
            await self._emit_result(ASREventType.PARTIAL_RESULT, "stale-v1")
        self._segment_number += 1

    async def _emit_result(
        self,
        event_type: ASREventType,
        version: str,
    ) -> None:
        segment_id = f"fake-asr-{self._segment_number:06d}"
        end_ms = self.audio_chunk_count * 100
        start_ms = max(
            0,
            end_ms - self.config.chunks_per_segment * 100,
        )
        await self._emit(
            event_type,
            segment_id=segment_id,
            text=f"fake transcript {self._segment_number} {version}",
            begin_time_ms=start_ms,
            end_time_ms=end_ms,
            confidence=1.0,
        )

    async def _emit(
        self,
        event_type: ASREventType,
        **fields,
    ) -> None:
        self._event_number += 1
        await self._events.put(
            ASREvent(
                event_type=event_type,
                provider_event_id=f"fake-asr-event-{self._event_number}",
                raw_payload={"provider": "fake"},
                **fields,
            )
        )

