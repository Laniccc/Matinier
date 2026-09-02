from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.transcription.chunker import AudioChunker
from app.translation.errors import (
    TranslationBackpressureError,
    TranslationStateError,
)
from app.translation.models import TranslationEvent, TranslationEventType, TranslationMetrics
from app.translation.provider import SpeechTranslationProvider


ProviderFactory = Callable[[], SpeechTranslationProvider]
EventHandler = Callable[[TranslationEvent], Awaitable[None]]
_QUEUE_STOP = object()


class TranslationSession:
    """Bounded audio delivery for one independent speech translation stream."""

    def __init__(
        self,
        *,
        provider_factory: ProviderFactory,
        queue_max_chunks: int = 20,
        chunk_duration_ms: int = 100,
        event_handler: EventHandler | None = None,
    ) -> None:
        if queue_max_chunks <= 0:
            raise ValueError("queue_max_chunks must be positive")
        self._provider_factory = provider_factory
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=queue_max_chunks
        )
        self._chunker = AudioChunker(chunk_duration_ms=chunk_duration_ms)
        self._event_handler = event_handler
        self._provider: SpeechTranslationProvider | None = None
        self._sender_task: asyncio.Task[None] | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._state = "idle"
        self._close_called = False
        self.metrics = TranslationMetrics()
        self._max_queue_size = 0

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def max_queue_size(self) -> int:
        return self._max_queue_size

    async def start(self) -> None:
        if self._state != "idle":
            raise TranslationStateError(
                "translation session can only be started once"
            )
        self._state = "starting"
        provider = self._provider_factory()
        self._provider = provider
        try:
            await provider.start()
        except BaseException:
            self._state = "failed"
            await provider.aclose()
            raise
        self._state = "started"
        self._event_task = asyncio.create_task(
            self._consume_events(provider),
            name="translation-events",
        )
        self._sender_task = asyncio.create_task(
            self._send_chunks(provider),
            name="translation-sender",
        )

    def send_frame(self, pcm: bytes | bytearray | memoryview) -> None:
        if self._state != "started":
            raise TranslationStateError(
                "translation session is not accepting audio"
            )
        if self._sender_task is not None and self._sender_task.done():
            error = self._sender_task.exception()
            if error is not None:
                raise error
            raise TranslationStateError("translation sender has stopped")
        for chunk in self._chunker.feed(pcm):
            try:
                self._queue.put_nowait(chunk)
                self._max_queue_size = max(
                    self._max_queue_size,
                    self._queue.qsize(),
                )
            except asyncio.QueueFull as error:
                raise TranslationBackpressureError(
                    "translation audio queue is full; audio was not silently dropped"
                ) from error

    async def finish(self) -> TranslationMetrics:
        if self._state == "completed":
            return self.metrics
        if self._state != "started":
            raise TranslationStateError(
                "translation session cannot finish in its current state"
            )
        self._state = "finishing"
        tail = self._chunker.flush()
        if tail is not None:
            try:
                self._queue.put_nowait(tail)
                self._max_queue_size = max(
                    self._max_queue_size,
                    self._queue.qsize(),
                )
            except asyncio.QueueFull as error:
                raise TranslationBackpressureError(
                    "translation audio queue is full while finishing"
                ) from error
        assert self._sender_task is not None
        assert self._provider is not None
        try:
            await self._finish_sender()
            await self._provider.finish()
            if self._event_task is not None:
                await self._event_task
            self._state = "completed"
            return self.metrics
        except BaseException:
            self._state = "failed"
            raise

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

    async def _send_chunks(
        self,
        provider: SpeechTranslationProvider,
    ) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _QUEUE_STOP:
                    return
                assert isinstance(item, bytes)
                await provider.send_audio(item)
                self.metrics.mark_audio_sent(len(item))
            finally:
                self._queue.task_done()

    async def _finish_sender(self) -> None:
        assert self._sender_task is not None
        sender = self._sender_task
        if sender.done():
            await sender
            return
        stop_enqueue = asyncio.create_task(
            self._queue.put(_QUEUE_STOP),
            name="translation-stop-enqueue",
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

    async def _consume_events(
        self,
        provider: SpeechTranslationProvider,
    ) -> None:
        async for event in provider.events():
            self.metrics.observe(event)
            if self._event_handler is not None:
                await self._event_handler(event)
            if event.event_type is TranslationEventType.STREAM_ERROR:
                return
