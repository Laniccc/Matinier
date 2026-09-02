from __future__ import annotations

import asyncio

import pytest

from app.worker.transport_runtime import (
    TransportBackpressureError,
    TransportFrameSink,
    consume_transport_audio,
)


class Frame:
    sample_rate = 16_000
    num_channels = 1
    samples_per_channel = 320
    data = b"\x01\x00" * 320


class Event:
    frame = Frame()


def test_transport_sink_captures_bounded_metrics_without_provider() -> None:
    async def scenario() -> None:
        now = [0.0]
        memory = [50.0]

        def read_memory() -> float:
            memory[0] += 0.25
            return memory[0]

        sink = TransportFrameSink(
            queue_max_frames=4,
            clock=lambda: now[0],
            memory_reader=read_memory,
            task_counter=lambda: 7,
        )
        await sink.start()
        await sink.send_frame(Frame())
        await asyncio.sleep(0)
        await sink.send_frame(Frame())
        now[0] = 0.04
        metrics = await sink.finish()
        await sink.aclose()

        assert metrics.frame_count == 2
        assert metrics.audio_bytes == 1_280
        assert metrics.audio_duration_ms == pytest.approx(40.0)
        assert metrics.receive_wall_time_ms == pytest.approx(40.0)
        assert metrics.realtime_ratio == pytest.approx(1.0)
        assert metrics.queue_size == 0
        assert 1 <= metrics.max_queue_size <= 2
        assert metrics.dropped_frames == 0
        assert metrics.peak_memory_mb >= metrics.start_memory_mb
        assert metrics.active_task_count_end == 7

    asyncio.run(scenario())


def test_transport_sink_fails_explicitly_when_slow_consumer_fills_queue() -> None:
    async def scenario() -> None:
        sink = TransportFrameSink(
            queue_max_frames=1,
            consumer_delay_seconds=0.1,
        )
        await sink.start()
        await sink.send_frame(Frame())
        with pytest.raises(TransportBackpressureError):
            await sink.send_frame(Frame())
        assert sink.metrics.dropped_frames == 1
        await sink.aclose()

    asyncio.run(scenario())


def test_transport_runtime_uses_audio_stream_and_closes_all_resources() -> None:
    async def scenario() -> None:
        class Stream:
            def __init__(self) -> None:
                self.events = iter([Event(), Event()])
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.events)
                except StopIteration as error:
                    raise StopAsyncIteration from error

            async def aclose(self) -> None:
                self.closed = True

        operations: list[object] = []

        class Runtime:
            source_type = "file"

            async def start_replay(self) -> None:
                operations.append("replaying")

            async def publish_progress(self, value: int) -> None:
                operations.append(("progress", value))

            async def begin_finalizing(self) -> None:
                operations.append("finalizing")

            async def complete(self, metrics) -> None:
                operations.append(("completed", metrics.sent_audio_bytes))

            async def fail(self, error: BaseException) -> None:
                operations.append(("failed", type(error).__name__))

            async def cancel(self) -> None:
                operations.append("cancelled")

            async def complete_source_cleanup(self) -> None:
                operations.append("source-cleaned")

            async def fail_source_cleanup(self, error: BaseException) -> None:
                operations.append(("source-cleanup-failed", type(error).__name__))

            async def aclose(self) -> None:
                operations.append("runtime-closed")

        stream = Stream()
        metrics = await consume_transport_audio(
            track="track",
            room_name="room",
            participant_identity="participant",
            session_id="session",
            stream_factory=lambda *args, **kwargs: stream,
            sink_factory=lambda: TransportFrameSink(queue_max_frames=4),
            caption_runtime=Runtime(),
        )

        assert metrics.frame_count == 2
        assert stream.closed is True
        assert operations == [
            "replaying",
            "finalizing",
            ("completed", 1_280),
            "source-cleaned",
            "runtime-closed",
        ]

    asyncio.run(scenario())
