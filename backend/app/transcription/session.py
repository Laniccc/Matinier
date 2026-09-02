from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.transcription.chunker import AudioChunker
from app.transcription.errors import (
    ASRBackpressureError,
    ASRError,
    ASRStateError,
)
from app.transcription.models import ASREvent, ASREventType, TranscriptionMetrics
from app.transcription.provider import SpeechRecognitionProvider


logger = logging.getLogger(__name__)
ProviderFactory = Callable[[], SpeechRecognitionProvider]
EventHandler = Callable[[ASREvent], Awaitable[None]]
_QUEUE_STOP = object()


class TranscriptionSession:
    """Bounded audio delivery and normalized event lifecycle for one ASR stream."""

    def __init__(
        self,
        *,
        provider_factory: ProviderFactory,
        queue_max_chunks: int = 20,
        chunk_duration_ms: int = 100,
        startup_retries: int = 1,
        event_handler: EventHandler | None = None,
        log_context: dict[str, Any] | None = None,
        clock_ms: Callable[[], float] | None = None,
        log_provider_payloads: bool = False,
    ) -> None:
        if queue_max_chunks <= 0:
            raise ValueError("queue_max_chunks must be positive")
        if startup_retries < 0:
            raise ValueError("startup_retries must be non-negative")
        self._provider_factory = provider_factory
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=queue_max_chunks
        )
        self._chunker = AudioChunker(chunk_duration_ms=chunk_duration_ms)
        self._startup_retries = startup_retries
        self._event_handler = event_handler
        self._log_context = dict(log_context or {})
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1_000)
        self._log_provider_payloads = log_provider_payloads
        self.metrics = TranscriptionMetrics()
        self._provider: SpeechRecognitionProvider | None = None
        self._sender_task: asyncio.Task[None] | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._state = "idle"
        self._close_called = False
        self._summary_logged = False
        self._active_provider_error_counted = False
        self._max_queue_size = 0

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def max_queue_size(self) -> int:
        return self._max_queue_size

    async def start(self) -> None:
        if self._state != "idle":
            raise ASRStateError("transcription session can only be started once")
        self._state = "starting"
        last_error: BaseException | None = None
        for attempt in range(self._startup_retries + 1):
            provider = self._provider_factory()
            self._provider = provider
            self._active_provider_error_counted = False
            try:
                await provider.start()
            except asyncio.CancelledError:
                await provider.aclose()
                self._state = "closed"
                raise
            except BaseException as error:
                last_error = error
                self._record_provider_error()
                await provider.aclose()
                if attempt >= self._startup_retries:
                    self._state = "failed"
                    self._log_summary()
                    raise
                continue

            self._state = "started"
            self._event_task = asyncio.create_task(
                self._consume_events(provider),
                name="transcription-events",
            )
            self._sender_task = asyncio.create_task(
                self._send_chunks(provider),
                name="transcription-sender",
            )
            return

        self._state = "failed"
        if last_error is not None:
            raise last_error
        raise ASRStateError("transcription session failed to create a provider")

    def send_frame(self, pcm: bytes | bytearray | memoryview) -> None:
        if self._state != "started":
            raise ASRStateError("transcription session is not accepting audio")
        if self._sender_task is not None and self._sender_task.done():
            error = self._sender_task.exception()
            if error is not None:
                raise error
            raise ASRStateError("transcription audio sender has stopped")
        for chunk in self._chunker.feed(pcm):
            self._enqueue_chunk(chunk)

    async def finish(self) -> TranscriptionMetrics:
        if self._state == "completed":
            return self.metrics
        if self._state != "started":
            if self._state == "failed" and self._sender_task is not None:
                error = self._sender_task.exception()
                if error is not None:
                    raise error
            raise ASRStateError("transcription session cannot finish in its current state")
        self._state = "finishing"
        tail = self._chunker.flush()
        if tail is not None:
            self._enqueue_chunk(tail)
        assert self._sender_task is not None
        assert self._provider is not None
        try:
            await self._finish_sender()
            await self._provider.finish()
            if self._event_task is not None:
                await self._event_task
            self._state = "completed"
            return self.metrics
        except asyncio.CancelledError:
            self._state = "failed"
            raise
        except BaseException:
            self._state = "failed"
            raise
        finally:
            self._log_summary()

    async def aclose(self) -> None:
        if self._close_called:
            return
        self._close_called = True
        tasks = tuple(
            task
            for task in (self._sender_task, self._event_task)
            if task is not None and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._provider is not None:
            await self._provider.aclose()
        if self._state != "completed":
            self._state = "closed"
        self._log_summary()

    def _enqueue_chunk(self, chunk: bytes) -> None:
        try:
            self._queue.put_nowait(chunk)
            self._max_queue_size = max(
                self._max_queue_size,
                self._queue.qsize(),
            )
        except asyncio.QueueFull as error:
            raise ASRBackpressureError(
                "transcription audio queue is full; audio was not silently dropped"
            ) from error

    async def _send_chunks(self, provider: SpeechRecognitionProvider) -> None:
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is _QUEUE_STOP:
                        return
                    assert isinstance(item, bytes)
                    self.metrics.mark_audio_started(self._clock_ms())
                    await provider.send_audio(item)
                    self.metrics.mark_audio_sent(len(item))
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._record_provider_error()
            raise

    async def _finish_sender(self) -> None:
        assert self._sender_task is not None
        sender = self._sender_task
        if sender.done():
            await sender
            return

        stop_enqueue = asyncio.create_task(
            self._queue.put(_QUEUE_STOP),
            name="transcription-stop-enqueue",
        )
        try:
            done, _ = await asyncio.wait(
                {sender, stop_enqueue},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sender in done:
                if not stop_enqueue.done():
                    stop_enqueue.cancel()
                    await asyncio.gather(stop_enqueue, return_exceptions=True)
                await sender
                return

            await stop_enqueue
            await sender
        finally:
            if not stop_enqueue.done():
                stop_enqueue.cancel()
                await asyncio.gather(stop_enqueue, return_exceptions=True)

    async def _consume_events(self, provider: SpeechRecognitionProvider) -> None:
        iterator = provider.events().__aiter__()
        while True:
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                raise
            except BaseException:
                self._record_provider_error()
                raise

            if event.event_type is ASREventType.STREAM_ERROR:
                self._record_provider_error()
            elif event.event_type in {
                ASREventType.PARTIAL_RESULT,
                ASREventType.FINAL_RESULT,
            }:
                self.metrics.observe(
                    event,
                    observed_at_ms=self._clock_ms(),
                )
            else:
                self.metrics.observe(event)
            if self._event_handler is not None:
                await self._event_handler(event)
            self._log_event(event)

    def _record_provider_error(self) -> None:
        if self._active_provider_error_counted:
            return
        self._active_provider_error_counted = True
        self.metrics.provider_error_count += 1

    def _log_event(self, event: ASREvent) -> None:
        event_fields = {
            "provider_event_id": event.provider_event_id,
            "segment_id": event.segment_id,
            "text": event.text,
            "is_final": event.is_final,
            "begin_time_ms": event.begin_time_ms,
            "end_time_ms": event.end_time_ms,
            "confidence": event.confidence,
        }
        if event.event_type is ASREventType.PARTIAL_RESULT:
            logger.info(
                "[partial] %s",
                event.text or "",
                extra=self._extra("asr_partial_result", **event_fields),
            )
        elif event.event_type is ASREventType.FINAL_RESULT:
            logger.info(
                "[final] %s",
                event.text or "",
                extra=self._extra("asr_final_result", **event_fields),
            )
        elif event.event_type is ASREventType.STREAM_STARTED:
            logger.info(
                "asr stream started",
                extra=self._extra("asr_stream_started", **event_fields),
            )
        elif event.event_type is ASREventType.STREAM_COMPLETED:
            logger.info(
                "asr stream completed",
                extra=self._extra("asr_stream_completed", **event_fields),
            )
        elif event.event_type is ASREventType.STREAM_ERROR:
            logger.error(
                "asr stream error",
                extra=self._extra(
                    "asr_stream_error",
                    error_type=ASRError.__name__,
                    **event_fields,
                ),
            )
        if self._log_provider_payloads and event.raw_payload is not None:
            logger.debug(
                "asr provider payload",
                extra=self._extra(
                    "asr_provider_payload",
                    raw_payload=event.raw_payload,
                    provider_event_id=event.provider_event_id,
                ),
            )

    def _log_summary(self) -> None:
        if self._summary_logged:
            return
        self._summary_logged = True
        logger.info(
            "asr session summary",
            extra=self._extra("asr_summary", **self.metrics.log_fields()),
        )

    def _extra(self, event: str, **fields: Any) -> dict[str, Any]:
        return {
            "process_name": "worker",
            "session_id": self._log_context.get("session_id"),
            "room_name": self._log_context.get("room_name"),
            "participant_identity": self._log_context.get("participant_identity"),
            "event": event,
            "error_type": fields.pop("error_type", None),
            **fields,
        }
