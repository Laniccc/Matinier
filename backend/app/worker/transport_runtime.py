from __future__ import annotations

import asyncio
import logging
import time
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import psutil
from livekit import rtc

from app.errors import error_category
from app.transcription.models import TranscriptionMetrics
from app.worker.audio_stats import AudioFrameStats


logger = logging.getLogger(__name__)
_QUEUE_STOP = object()


class TransportBackpressureError(RuntimeError):
    """Raised when the bounded Frame Sink cannot accept another frame."""


def process_memory_mb() -> float:
    return psutil.Process().memory_info().rss / (1024 * 1024)


def active_task_count() -> int:
    return sum(1 for task in asyncio.all_tasks() if not task.done())


@dataclass(slots=True)
class TransportMetrics:
    audio: AudioFrameStats = field(default_factory=AudioFrameStats)
    queue_size: int = 0
    max_queue_size: int = 0
    dropped_frames: int = 0
    receive_wall_time_ms: float = 0.0
    start_memory_mb: float = 0.0
    end_memory_mb: float = 0.0
    peak_memory_mb: float = 0.0
    active_task_count_end: int = 0

    @property
    def frame_count(self) -> int:
        return self.audio.frame_count

    @property
    def audio_bytes(self) -> int:
        return self.audio.audio_bytes

    @property
    def audio_duration_ms(self) -> float:
        return self.audio.audio_duration_seconds * 1_000

    @property
    def realtime_ratio(self) -> float | None:
        if self.receive_wall_time_ms <= 0:
            return None
        return self.audio_duration_ms / self.receive_wall_time_ms

    @property
    def process_memory_mb(self) -> float:
        return self.peak_memory_mb

    def log_fields(self) -> dict[str, int | float | None]:
        return {
            "frame_count": self.frame_count,
            "audio_bytes": self.audio_bytes,
            "audio_duration_ms": round(self.audio_duration_ms, 3),
            "receive_wall_time_ms": round(self.receive_wall_time_ms, 3),
            "realtime_ratio": (
                round(self.realtime_ratio, 6)
                if self.realtime_ratio is not None
                else None
            ),
            "queue_size": self.queue_size,
            "max_queue_size": self.max_queue_size,
            "dropped_frames": self.dropped_frames,
            "process_memory_mb": round(self.process_memory_mb, 3),
            "active_task_count": self.active_task_count_end,
        }


class TransportFrameSink:
    """Bounded no-provider sink used to exercise the Worker audio transport."""

    def __init__(
        self,
        *,
        queue_max_frames: int = 100,
        consumer_delay_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        memory_reader: Callable[[], float] = process_memory_mb,
        task_counter: Callable[[], int] = active_task_count,
    ) -> None:
        if queue_max_frames <= 0:
            raise ValueError("queue_max_frames must be positive")
        if consumer_delay_seconds < 0:
            raise ValueError("consumer_delay_seconds must be non-negative")
        self.metrics = TransportMetrics()
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=queue_max_frames
        )
        self._consumer_delay_seconds = consumer_delay_seconds
        self._clock = clock
        self._memory_reader = memory_reader
        self._task_counter = task_counter
        self._consumer_task: asyncio.Task[None] | None = None
        self._started_at: float | None = None
        self._closed = False
        self._finished = False

    async def start(self) -> None:
        if self._consumer_task is not None:
            raise RuntimeError("transport sink can only be started once")
        self._started_at = self._clock()
        initial_memory = self._memory_reader()
        self.metrics.start_memory_mb = initial_memory
        self.metrics.end_memory_mb = initial_memory
        self.metrics.peak_memory_mb = initial_memory
        self._consumer_task = asyncio.create_task(
            self._consume(),
            name="transport-frame-sink",
        )

    async def send_frame(self, frame: Any) -> None:
        if self._consumer_task is None or self._finished or self._closed:
            raise RuntimeError("transport sink is not accepting frames")
        self.metrics.audio.observe(frame)
        self._sample_memory()
        try:
            self._queue.put_nowait(bytes(frame.data))
        except asyncio.QueueFull as error:
            self.metrics.dropped_frames += 1
            self.metrics.queue_size = self._queue.qsize()
            raise TransportBackpressureError(
                "transport frame queue is full; the frame was not silently dropped"
            ) from error
        self.metrics.queue_size = self._queue.qsize()
        self.metrics.max_queue_size = max(
            self.metrics.max_queue_size,
            self.metrics.queue_size,
        )

    async def finish(self) -> TransportMetrics:
        if self._finished:
            return self.metrics
        if self._consumer_task is None:
            raise RuntimeError("transport sink has not started")
        await self._queue.put(_QUEUE_STOP)
        await self._consumer_task
        self._finished = True
        self._finalize_metrics()
        return self.metrics

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
            await asyncio.gather(self._consumer_task, return_exceptions=True)
        self._finalize_metrics()

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _QUEUE_STOP:
                    return
                if self._consumer_delay_seconds:
                    await asyncio.sleep(self._consumer_delay_seconds)
            finally:
                self._queue.task_done()
                self.metrics.queue_size = self._queue.qsize()
                self._sample_memory()

    def _sample_memory(self) -> None:
        current = self._memory_reader()
        self.metrics.end_memory_mb = current
        self.metrics.peak_memory_mb = max(
            self.metrics.peak_memory_mb,
            current,
        )

    def _finalize_metrics(self) -> None:
        self._sample_memory()
        if self._started_at is not None:
            self.metrics.receive_wall_time_ms = max(
                0.0,
                (self._clock() - self._started_at) * 1_000,
            )
        self.metrics.queue_size = self._queue.qsize()
        self.metrics.active_task_count_end = self._task_counter()


async def _iter_audio_events_until_stop(
    stream: Any,
    stop_event: asyncio.Event | None,
) -> AsyncIterator[Any]:
    if stop_event is None:
        async for event in stream:
            yield event
        return

    iterator = stream.__aiter__()
    stop_task = asyncio.create_task(
        stop_event.wait(),
        name="transport-audio-stop-waiter",
    )
    frame_task: asyncio.Task[Any] | None = None
    try:
        while True:
            frame_task = asyncio.create_task(
                anext(iterator),
                name="transport-audio-next-frame",
            )
            if stop_task.done():
                await asyncio.sleep(0)
            done, _ = await asyncio.wait(
                (frame_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if frame_task in done:
                try:
                    event = frame_task.result()
                except StopAsyncIteration:
                    return
                frame_task = None
                yield event
                continue
            frame_task.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)
            frame_task = None
            return
    finally:
        if frame_task is not None and not frame_task.done():
            frame_task.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)
        if not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)


async def consume_transport_audio(
    *,
    track: Any,
    room_name: str,
    participant_identity: str,
    session_id: str,
    stop_event: asyncio.Event | None = None,
    stream_factory: Callable[..., Any] = rtc.AudioStream,
    sink_factory: Callable[[], TransportFrameSink] = TransportFrameSink,
    caption_runtime: Any | None = None,
    source_abort_handler: Callable[[str, str], Awaitable[None]] | None = None,
    track_sid: str | None = None,
) -> TransportMetrics:
    stream = stream_factory(
        track,
        sample_rate=16_000,
        num_channels=1,
        frame_size_ms=20,
    )
    sink = sink_factory()
    error_type: str | None = None
    started_at_ms = time.monotonic() * 1_000
    try:
        await sink.start()
        if caption_runtime is not None:
            await caption_runtime.start_replay()
        async for event in _iter_audio_events_until_stop(stream, stop_event):
            await sink.send_frame(event.frame)
            await asyncio.sleep(0)
            if sink.metrics.frame_count % 25 == 0 and caption_runtime is not None:
                await caption_runtime.publish_progress(
                    int(round(sink.metrics.audio_duration_ms))
                )
            if sink.metrics.frame_count in {1} or sink.metrics.frame_count % 250 == 0:
                logger.info(
                    "transport-only audio progress",
                    extra={
                        "process_name": "worker",
                        "session_id": session_id,
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "event": "transport_audio_progress",
                        **sink.metrics.log_fields(),
                    },
                )
        metrics = await sink.finish()
        if caption_runtime is not None:
            await caption_runtime.begin_finalizing()
            await caption_runtime.complete(
                TranscriptionMetrics(
                    sent_audio_chunk_count=metrics.frame_count,
                    sent_audio_bytes=metrics.audio_bytes,
                )
            )
        return metrics
    except BaseException as error:
        error_type = error_category(error, default="transport_stream_error")
        if caption_runtime is not None:
            try:
                if isinstance(error, asyncio.CancelledError):
                    await caption_runtime.cancel()
                else:
                    await caption_runtime.fail(error)
            except BaseException as report_error:
                logger.error(
                    "transport failure reporting failed",
                    extra={
                        "process_name": "worker",
                        "session_id": session_id,
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "event": "transport_failure_reporting_failed",
                        "internal_error_type": type(report_error).__name__,
                    },
                )
        if (
            source_abort_handler is not None
            and getattr(caption_runtime, "source_type", None) == "hls"
        ):
            abort_reason = (
                "worker_cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "caption_runtime_error"
            )
            try:
                await source_abort_handler(
                    abort_reason,
                    str(error) or type(error).__name__,
                )
            except BaseException as abort_error:
                logger.error(
                    "transport source abort request failed",
                    extra={
                        "process_name": "worker",
                        "session_id": session_id,
                        "room_name": room_name,
                        "participant_identity": participant_identity,
                        "event": "source_abort_failed",
                        "failure_code": abort_reason,
                        "cleanup_status": "pending",
                        "internal_error_type": type(abort_error).__name__,
                    },
                )
        raise
    finally:
        try:
            await sink.aclose()
        finally:
            try:
                await stream.aclose()
            except BaseException as cleanup_error:
                if caption_runtime is not None:
                    await caption_runtime.fail_source_cleanup(cleanup_error)
                raise
            else:
                if caption_runtime is not None:
                    await caption_runtime.complete_source_cleanup()
            finally:
                if caption_runtime is not None:
                    await caption_runtime.aclose()
        logger.info(
            "transport-only audio complete",
            extra={
                "process_name": "worker",
                "session_id": session_id,
                "room_name": room_name,
                "participant_identity": participant_identity,
                "track_sid": track_sid,
                "source_type": getattr(caption_runtime, "source_type", None),
                "event": "worker_cleanup_completed",
                "error_type": error_type,
                "failure_code": error_type,
                "cleanup_status": "completed",
                "status": "failed" if error_type is not None else "completed",
                "elapsed_ms": round(
                    time.monotonic() * 1_000 - started_at_ms,
                    3,
                ),
                **sink.metrics.log_fields(),
            },
        )
